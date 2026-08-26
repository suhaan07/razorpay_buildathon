from app.matching.resolver import normalize, resolve


def test_normalize_strips_legal_suffixes():
    assert normalize("Alpha Textiles Pvt Ltd") == "alpha textiles"
    assert normalize("Gamma Technologies Private Limited") == "gamma technologies"
    assert normalize("Beta Logistics LLC") == "beta logistics"


def test_normalize_does_not_truncate_names_containing_suffix_letters():
    assert normalize("Omicron Traders") == "omicron traders"


def test_normalize_repeats_until_stable():
    assert normalize("Zeta Hardware Corp - Customer") == "zeta hardware"


def test_resolve_matched():
    candidates = ["Alpha Textiles Pvt Ltd", "Beta Logistics", "Gamma Technologies Private Limited"]
    result = resolve("alpha textiles", candidates)
    assert result.status == "matched"
    assert result.customer_name == "Alpha Textiles Pvt Ltd"


def test_resolve_not_found():
    candidates = ["Alpha Textiles Pvt Ltd", "Beta Logistics"]
    result = resolve("completely unrelated corp", candidates)
    assert result.status == "not_found"


def test_resolve_ambiguous():
    candidates = ["Kumar Enterprises Mumbai", "Kumar Enterprises Delhi"]
    result = resolve("Kumar Enterprises", candidates)
    assert result.status == "ambiguous"
    assert len(result.candidates) >= 2
