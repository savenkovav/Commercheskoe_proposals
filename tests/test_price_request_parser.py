from pathlib import Path

from src.services.matcher import ItemMatcher
from src.services.models import PriceListItem, TZItem
from src.services.tz_parser import _parse_price_request_text, parse_tz
from src.services.tz_search import extract_vendor_codes

_SAMPLE_TEXT = """
TIpocum Bac mpeyocrasuTb weHoBy10 Hiopmaljuio (KoMMepueckoe TIpe/IOKeHMe)

No Haumenosanue Vs06paxenne Lena 3a en. Kon-Bo Cymma
wn (py6.) (ur) (py6.)

1 |LER2938 "Po6or bormm 1
Jemoxc. Bepcusa 2.0" (78
'9JIEMEHTOB)

2 |LER2939 Paspuparomat
urpyuika "AKceccyapbi
dua pobota Borm.
Crpountes" (10
9II@MCHTOB C
KapTOuKaMn)

3 |RM6001 Kommexr
TeMaTHYeCKHX Hoel
"CrpouM MapulpyTBI c
Po6oMsuupt0" (6
9JIEMEHTOB)

uTOTO
"""

PDF_PATH = Path(
    "/Users/aleksandrsavenkov/Desktop/Проект КП/Образцы/Примеры запросов/"
    "26-06-2026_22-10-38/Ценовой запрос на робоигрушки.pdf"
)


def test_parse_price_request_text_extracts_three_items() -> None:
    items = _parse_price_request_text(_SAMPLE_TEXT)
    assert len(items) == 3
    assert items[0].number == 1
    assert "LER2938" in items[0].name
    assert "Po6or bormm" in items[0].name
    assert items[0].quantity == 1.0
    assert items[0].specifications and "LER2938" in items[0].specifications
    assert "78 элементов" in items[0].specifications
    assert items[1].number == 2
    assert "LER2939" in items[1].name
    assert items[1].quantity == 1.0
    assert "10 элементов" in items[1].specifications
    assert items[2].number == 3
    assert "RM6001" in items[2].name
    assert items[2].quantity == 1.0
    assert "6 элементов" in items[2].specifications


def test_parse_price_request_pdf_file() -> None:
    if not PDF_PATH.exists():
        return
    items = parse_tz(PDF_PATH)
    assert len(items) == 3
    codes = {extract_vendor_codes(item.name, item.specifications)[0] for item in items}
    assert codes == {"LER2938", "LER2939", "RM6001"}


def test_matcher_finds_price_list_by_vendor_code() -> None:
    matcher = ItemMatcher(
        catalog=[],
        registry=[],
        price_lists=[
            PriceListItem(
                code="LER2938",
                name='Робот Botley 2.0 "Детская версия"',
                price=12500.0,
                sheet="LR",
                supplier="Learning Resources",
            )
        ],
    )
    tz_item = TZItem(
        number=1,
        name='LER2938 "Робот Botley 2.0"',
        unit="шт.",
        quantity=1.0,
        specifications="Код производителя: LER2938; Элементов в комплекте: 78",
    )
    candidates = matcher.find_candidates(tz_item)
    assert candidates["price"]
    assert candidates["price"][0].score == 100.0
    assert "Botley" in candidates["price"][0].name
