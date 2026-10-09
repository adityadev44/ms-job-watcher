from src import sonatype_fetcher as f

def test_lever_skill_bullets_and_country_locations():
    description = f._build_description({'descriptionPlain': 'Intro', 'lists': [{'text': 'Requirements', 'content': '<ul><li>generative ai and Python</li></ul>'}]})
    assert 'generative ai' in description
    assert f._location_from_posting({'country': 'IN', 'categories': {'location': 'Hyderabad'}}) == 'Hyderabad, India'
    assert 'India' not in f._location_from_posting({'country': 'US', 'categories': {'location': 'Boston'}})
    assert f._location_from_posting({'categories': {}}) == ''
