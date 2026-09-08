from src import apexgroup_fetcher as f
def test_verified_workday_tenant_and_india_facet():
    assert f._TENANT == "theapexgroup"
    assert f._SITE == "apexgroupcareers"
    assert f._INDIA == "c4f78be1a8f14da0ab49ce1162348a5e"
