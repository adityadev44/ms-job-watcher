from src import citco_fetcher as f
def test_verified_oracle_site_and_india_facet():
    assert f._SITE == "CX_1"
    assert f._INDIA == 300000000431886
    assert "oraclecloud.com" in f._BASE
