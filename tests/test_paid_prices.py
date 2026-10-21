from app.dialogue.prices import load_paid_prices


def test_paid_prices_load_and_normalize_from_yaml(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text(
        "prices:\n  - service: 'УЗИ щитовидной железы'\n    price: 1300\n", encoding="utf-8"
    )

    assert load_paid_prices(path) == {"узи щитовидной железы": 1300}


def test_missing_price_source_disables_direct_price_answers():
    assert load_paid_prices(None) == {}
