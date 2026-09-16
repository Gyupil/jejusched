"""네임스페이스에 기대지 않는 XML 탐색 헬퍼.

hwpx는 `hp:` `hh:` `hs:` 접두사를 쓰지만 판본마다 URI가 달라질 수 있다.
그래서 어디서도 정규화된 이름(`{uri}tag`)을 쓰지 않고 **로컬 이름**으로만 찾는다.
"""

from __future__ import annotations

from typing import Iterator
from xml.etree.ElementTree import Element


def local(elem: Element) -> str:
    """`{uri}tag` → `tag`. 네임스페이스가 없으면 그대로."""
    tag = elem.tag
    if not isinstance(tag, str):  # 주석·PI 노드
        return ""
    return tag.rsplit("}", 1)[-1]


def children(elem: Element | None, name: str) -> list[Element]:
    """로컬 이름이 `name`인 **직계** 자식들. 순서는 문서 순서 그대로."""
    if elem is None:
        return []
    return [c for c in elem if local(c) == name]


def child(elem: Element | None, name: str) -> Element | None:
    """로컬 이름이 `name`인 첫 직계 자식."""
    found = children(elem, name)
    return found[0] if found else None


def descendants(elem: Element, name: str) -> Iterator[Element]:
    """로컬 이름이 `name`인 모든 후손(자기 자신 포함)."""
    for node in elem.iter():
        if local(node) == name:
            yield node


def attr_int(elem: Element | None, name: str, default: int) -> int:
    """정수 속성. 없거나 숫자가 아니면 `default`."""
    if elem is None:
        return default
    try:
        return int(elem.get(name, default))
    except (TypeError, ValueError):
        return default
