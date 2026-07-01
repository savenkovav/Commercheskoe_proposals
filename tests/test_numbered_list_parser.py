from src.services.tz_parser import _parse_numbered_list_text, parse_tz

_SAMPLE_LIST = """
1. Генератор сигналов звуковой и ультразвуковой частоты.
2 2. . Магнитные стрелки на подставках.
3. Манометр жидкostной демонстрационный.
4. Машина электрофорная.
5. Прибор для демонстрации правила Ленца.
6. Комплект стаканов химических (15 штук).
7. Термометр лабораторный (-10 С до +110 С).
8. Источник постоянного и переменного напряжения 10А (Блок питания 24 В
регулируемый).
9 9. . Амперметр с гальванометром демонстрационный. Амперметр-вольтметр с
гальванометром
10. Комплект гипсовых моделей геометрических тел.
11. Комплект моделей для натюрморта.
12. Комплект гипсовых моделей головы.
13. Комплект портретов отечественных и зарубежных художников.
14. Комплект демонстрационных учебных таблиц по изобразительному
искусству.
15. Комплект демонстрационных учебных таблиц по изобразительному
искусству.
Базовый (практический) комплект
МУЗ Шкаф для хранения учебных пособий (838-Приказ).
"""

_COVER_LETTER = """
Запрос о предоставлении ценовой информации
Просим Вас предоставить коммерческое предложение на товар
Приложение: 1. Описание объекта закупки;
2. Форма коммерческого предложения.
"""


def test_parse_numbered_equipment_list() -> None:
    items = _parse_numbered_list_text(_SAMPLE_LIST)
    assert len(items) == 16
    assert items[0].number == 1
    assert "генератор" in items[0].name.lower()
    assert items[0].quantity == 1.0
    assert items[1].number == 2
    assert "магнитные" in items[1].name.lower()
    assert items[5].number == 6
    assert "стаканов" in items[5].name.lower()
    assert items[5].quantity == 1.0
    assert items[8].number == 9
    assert "амперметр" in items[8].name.lower()
    assert items[15].number == 16
    assert "шкаф" in items[15].name.lower()


def test_numbered_list_skips_cover_letter_appendix() -> None:
    items = _parse_numbered_list_text(_COVER_LETTER)
    assert items == []


def test_uchtenader_request_pdf_is_cover_letter_only() -> None:
    from pathlib import Path

    pdf_path = Path(
        "/Users/aleksandrsavenkov/Desktop/Проект КП/Образцы/Примеры запросов/"
        "26-06-2026_22-10-38/Запрос 1195_ООО Учтендер.pdf"
    )
    if not pdf_path.exists():
        return
    try:
        parse_tz(pdf_path)
        assert False, "expected ValueError for cover letter PDF"
    except ValueError as exc:
        assert "сопроводительное письмо" in str(exc).lower()
