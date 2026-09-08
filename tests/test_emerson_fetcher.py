from src import emerson_fetcher as f
def test_verified_source_and_india_facet():
    assert f._SITE == "CX_1"
    assert f._INDIA == 300000000228786
