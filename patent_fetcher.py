import json
import logging

# import re
from argparse import ArgumentParser
from collections.abc import Iterator
from typing import Any, TypeAlias, cast

import rich
from bs4 import BeautifulSoup, Tag
from rich.logging import RichHandler
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

logger = logging.getLogger(__name__)

Field: TypeAlias = tuple[str, Any]
FieldIterator: TypeAlias = Iterator[Field]


def fetch_html(url: str) -> str:
    options = Options()
    options.add_argument("headless")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    driver.get(f"view-source:{url}")
    request_id = ""
    for entry in driver.get_log("performance"):  # type: ignore
        message = json.loads(entry["message"])["message"]
        if message["method"] == "Network.loadingFinished":
            request_id = message["params"]["requestId"]
            break

    if not request_id:
        raise RuntimeError(
            "Could not find 'Network.loadingFinished' message in browser logs."
        )
    # Documentation for getResponseBody command:
    # https://chromedevtools.github.io/devtools-protocol/tot/Network/#method-getResponseBody
    response = driver.execute_cdp_cmd(
        "Network.getResponseBody", {"requestId": request_id}
    )
    if response["base64Encoded"]:
        # We raise an error because the documentation doesn't specify what form
        # of base64 is used, and we haven't encountered this situation yet.
        raise NotImplementedError("Encountered base64-encoded content.")
    return cast(str, response["body"])


def parse(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, features="html.parser")
    root = soup.html
    assert root

    article = root.find("article")
    assert isinstance(article, Tag)

    data = dict[str, Any]()
    parse_tag(article, data)
    parse_sections(article, data)
    return data


Node: TypeAlias = dict[str, Any]


def parse_label(tag: Tag) -> str:
    raw = tag.string
    if not isinstance(raw, str):
        logger.warning("Label tag has no string")
        return ""
    raw = raw.strip()

    parts = list[str]()
    for i, part in enumerate(raw.split()):
        if not part[0].isalnum():
            break
        if i == 0:
            parts.append(part.lower())
        else:
            parts.append(part.capitalize())
    return "".join(parts)


START_TAGS = ("dt", "h2")


def parse_children(tag: Tag, current_node: Node) -> None:
    for child in tag.children:
        if not isinstance(child, Tag):
            continue
        parse_tag(child, current_node)


def parse_siblings(tag: Tag, current_node: Node) -> None:
    for sibling in tag.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if sibling.name in START_TAGS:
            return
        parse_tag(sibling, current_node)


hack = set[int]()


def property_value(property_name: str, tag: Tag) -> Any:
    if tag.has_attr("itemscope"):
        child_node: Node = {}
        parse_children(tag, child_node)
        return child_node
    if content := tag.get("content"):
        return content
    if href := tag.get("href"):
        return href
    if src := tag.get("src"):
        return src
    if property_name == "content":
        # Sections such as "abstract" and "description" have nested tags
        # describing the content
        return tag.get_text(strip=True)
    text = tag.string
    if not isinstance(text, str):
        logger.warning(f"Skipping tag with no .string: {tag=}")
        return None
    return text.strip()


def parse_tag(tag: Tag, current_node: Node) -> None:  # noqa: C901
    if id(tag) in hack:
        return
    hack.add(id(tag))
    # logger.debug(f"{tag.name=} {tag.attrs=} {tag.sourceline=}")
    child_node: Node
    if tag.name in START_TAGS:
        label = parse_label(tag)
        logger.debug(f"starting new property: {label}")
        child_node = {}
        parse_siblings(tag, child_node)
        current_node[label] = child_node
        logger.debug(f"ending new property: {label}")
        return

    property_name = tag.get("itemprop")
    if not property_name:
        # This tag itself is not a property, but its descendants might be
        parse_children(tag, current_node)
        return
    assert isinstance(property_name, str)

    if tag.name == "section":
        # Will be handled by parse_sectoins() later
        return

    # if parse_special_section(property_name, tag):
    #     return

    value = property_value(property_name, tag)

    if tag.has_attr("repeat"):
        if property_name not in current_node:
            current_node[property_name] = []
        try:
            current_node[property_name].append(value)
        except Exception as e:
            logger.warning(f"{current_node=} {property_name=}")
            raise e
    else:
        current_node[property_name] = value


def parse_sections(article: Tag, current_node: Node) -> None:
    def is_section(tag: Tag) -> bool:
        return tag.name == "section" and tag.has_attr("itemscope")

    for section in article.find_all(is_section):
        property_name = section["itemprop"]
        assert isinstance(property_name, str)
        value: Any
        match property_name:
            case "abstract":
                value = dict(parse_abstract(section))
            case "description":
                value = dict(parse_description(section))
            case "claims":
                value = dict(parse_claims(section))
            case _:
                logger.warning(f"Unhandled section: {section.attrs=}")
                value = None
        current_node[property_name] = value


def tag_string(tag: Tag) -> str:
    return f"{tag.name=} {tag.attrs=} {tag.sourceline=}"


def parse_abstract(section: Tag) -> FieldIterator:
    abstract = section.find("abstract")
    assert isinstance(abstract, Tag)

    yield from abstract.attrs.items()
    yield "content", abstract.get_text(strip=True)


def parse_description(section: Tag) -> FieldIterator:
    description = section.find(attrs={"class": "description"})
    assert isinstance(description, Tag)

    for key, value in description.attrs.items():
        if key == "class":
            continue
        yield key, value

    def is_target(tag: Tag) -> bool:
        classes = tag.get("class") or []
        return tag.name == "heading" or "description-line" in classes

    def new_part(heading: str) -> Node:
        return {"heading": heading, "lines": []}

    parts = list[Node]()
    current_part = new_part(heading="")

    for tag in description.find_all(is_target):
        assert isinstance(tag, Tag)

        text = tag.get_text(strip=True)
        if tag.name == "heading":
            parts.append(current_part)
            current_part = new_part(heading=text)
            continue

        current_part["lines"].append({"num": tag.get("num"), "text": text})

    parts.append(current_part)

    yield "parts", parts


def has_class(tag: Tag, class_name: str) -> bool:
    classes = tag.get("class") or []
    return class_name in classes


def parse_claims(section: Tag) -> FieldIterator:
    claims_tag = section.find(lambda tag: has_class(tag, "claims"))
    assert isinstance(claims_tag, Tag)
    for key, value in claims_tag.attrs.items():
        if key == "class":
            continue
        yield key, value
        # logger.debug(list(claims.stripped_strings))

    parsed_claims = list[Node]()
    for claim in find_claims(claims_tag):
        assert isinstance(claim, Tag)
        parsed_claims.append(dict(parse_claim(claim)))

    yield "claims", parsed_claims


def attrs_except_class(tag: Tag) -> FieldIterator:
    for key, value in tag.attrs.items():
        if key != "class":
            yield key, value


def find_claims(claims_tag: Tag) -> Iterator[Tag]:
    # Different patent pages have different nesting structure for tags with the
    # class "claim". So to find the correct tags in a unified way, we find the
    # tags with the class "claim-text", and return all unique ancestor tags with
    # the class "claim".
    seen_tags = set[int]()
    for text_tag in claims_tag.find_all(lambda t: has_class(t, "claim-text")):
        claim = text_tag.find_parent(lambda t: has_class(t, "claim"))
        assert isinstance(claim, Tag)

        if id(claim) not in seen_tags:
            yield claim
        seen_tags.add(id(claim))


def parse_claim(claim: Tag) -> FieldIterator:
    yield from attrs_except_class(claim)
    yield "text", claim.get_text(strip=True)


# def parse_publication_number(body: Tag) -> FieldIterator:
#     publication_number = find_dt_tag(body, "Publication number")

#     # Parse all of the publication number values
#     values = list[str]()
#     for span in publication_number.find_next_siblings("span"):
#         assert isinstance(span, Tag)
#         if value := cast(str, span.string).strip():
#             values.append(value)
#     yield "values", values


def main() -> None:
    rich.reconfigure(stderr=True)
    log_handler = RichHandler(rich_tracebacks=True)
    file_handler = logging.FileHandler("log.txt", mode="w")
    logging.basicConfig(
        level="NOTSET",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[log_handler, file_handler],
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(logging.Filter(__name__))

    parser = ArgumentParser(
        description="Fetch JSON-encoded information about a patent."
    )
    parser.add_argument(
        "id_or_url",
        type=str,
        help=(
            "The Google Patent ID to fetch data for. "
            "If a URL is specified instead, then we fetch data from that URL "
            "instead of forming the URLs automatically. "
            "This is useful for debugging (e.g. fetching from a local file:// URL)."
        ),
    )

    args = parser.parse_args()

    url: str
    if "/" in args.id_or_url:
        url = args.id_or_url
    else:
        patent_id = args.id_or_url
        url = f"https://patents.google.com/patent/{patent_id}"

    parsed = parse(fetch_html(url))
    print(json.dumps(parsed, indent=2))


if __name__ == "__main__":
    pass
