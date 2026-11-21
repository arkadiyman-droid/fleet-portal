"""
tests/test_property_people_db.py — everyone under a property, and what each of
them may do there (Postgres tier)

A property's People page lists every person with a grant on it, on its client
or on one of its buildings, with three rights each: sees the property, runs
its fans, changes its heating settings. The settings right is offered on every
property; the two rights need the view and go with it.
"""
import pytest

from tests.conftest import requires_postgres
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

pytestmark = requires_postgres


@pytest.fixture
def people(app, world, session):
    """Two more people: Sam (a grant on one Berry building only) and Lee (no grants)."""
    import app.models as m
    from werkzeug.security import generate_password_hash
    b = m.Building.query.filter_by(name="440 Berry St").one()
    sam = m.User(name="Sam Tenant", email="sam@example.test", login="sam", password_hash=generate_password_hash("x" * 12))
    lee = m.User(name="Lee Manager", email="lee@example.test", login="lee", password_hash=generate_password_hash("x" * 12))
    session.add_all([sam, lee])
    session.flush()
    session.add(m.Permission(user_id=sam.id, scope_type="building", scope_id=b.id, role="viewer"))
    session.commit()
    return {"sam": sam.id, "lee": lee.id}


def _url(world):
    return f"/properties/{world['berry']}/people"


def test_the_page_lists_everyone_under_the_property_with_their_rights(root, world, people):
    page = root.get(_url(world)).get_data(as_text=True)
    assert "People — Berry" in page and "Pat Viewer" in page and "Sam Tenant" in page
    assert "sees only 440 Berry St" in page                   # a building grant, not the property
    assert "Lee Manager" not in page.split("Add a person")[0] and '<option value="%d">Lee Manager' % people["lee"] in page
    assert f'name="view_{world["viewer"]}" value="1" style="width:auto;margin:0" data-who="{world["viewer"]}" class="pv" checked' in page
    assert "Runs the fans" in page and "Changes heating settings" in page
    assert 'People</a>' in root.get("/properties").get_data(as_text=True)


def test_rights_are_given_and_taken_per_person(root, world, people):
    import app.models as m
    ids = [world["viewer"], people["sam"]]
    r = root.post(_url(world), data={"listed": ids, f"view_{world['viewer']}": "1", f"fans_{world['viewer']}": "1",
                                     f"sets_{world['viewer']}": "1", f"sets_{people['sam']}": "1"}, follow_redirects=True)
    said = r.get_data(as_text=True)
    assert "Pat Viewer may now run the fans" in said and "Pat Viewer may now change heating settings" in said
    assert "Not given, because the person does not see the whole property: Sam Tenant (change heating settings)" in said
    assert {(c.user_id, c.property_id) for c in m.SettingControl.query.all()} == {(world["viewer"], world["berry"])}
    assert {(c.user_id, c.property_id) for c in m.FanControl.query.all()} == {(world["viewer"], world["berry"])}

    # Sam is given the property, then the settings right, in one save
    root.post(_url(world), data={"listed": ids, f"view_{world['viewer']}": "1", f"fans_{world['viewer']}": "1",
                                 f"sets_{world['viewer']}": "1", f"view_{people['sam']}": "1", f"sets_{people['sam']}": "1"})
    assert m.SettingControl.query.filter_by(user_id=people["sam"]).count() == 1
    assert m.Permission.query.filter_by(user_id=people["sam"], scope_type="property", scope_id=world["berry"]).count() == 1

    # the view taken from Pat: both of Pat's rights go with it
    root.post(_url(world), data={"listed": ids, f"view_{people['sam']}": "1", f"sets_{people['sam']}": "1"})
    assert m.FanControl.query.count() == 0 and m.SettingControl.query.filter_by(user_id=world["viewer"]).count() == 0
    assert m.Permission.query.filter_by(user_id=world["viewer"], scope_type="property").count() == 0
    assert f'name="listed" value="{world["viewer"]}"' not in root.get(_url(world)).get_data(as_text=True)


def test_a_person_is_added_from_the_page(root, world, people):
    import app.models as m
    r = root.post(_url(world), data={"listed": [world["viewer"]], f"view_{world['viewer']}": "1", "add_user": str(people["lee"])},
                  follow_redirects=True)
    assert "Lee Manager added: can see Berry" in r.get_data(as_text=True)
    assert m.Permission.query.filter_by(user_id=people["lee"], scope_type="property", scope_id=world["berry"], role="viewer").count() == 1
    page = root.get(_url(world)).get_data(as_text=True)
    assert f'name="sets_{people["lee"]}"' in page


def test_a_grant_through_the_client_is_shown_and_left_alone(root, world, people, session):
    import app.models as m
    berry = session.get(m.Property, world["berry"])
    session.add(m.Permission(user_id=people["lee"], scope_type="client", scope_id=berry.client_id, role="operator"))
    session.commit()
    page = root.get(_url(world)).get_data(as_text=True)
    assert "through Owner A (operator)" in page
    root.post(_url(world), data={"listed": [people["lee"]], f"sets_{people['lee']}": "1"})      # its view box is not sent
    assert m.Permission.query.filter_by(user_id=people["lee"], scope_type="client").count() == 1
    assert m.SettingControl.query.filter_by(user_id=people["lee"]).count() == 1


def test_only_the_administrator(pat, world):
    assert pat.get(_url(world)).status_code == 403
    assert pat.post(_url(world), data={"listed": [world["viewer"]], f"sets_{world['viewer']}": "1"}).status_code == 403


def test_the_settings_tick_is_offered_on_every_property(root, world, session):
    import app.models as m
    bare = m.Property(client_id=session.get(m.Property, world["berry"]).client_id, name="Empty Lot")
    session.add(bare)
    session.commit()
    page = root.get(f"/users/{world['viewer']}/permissions").get_data(as_text=True)
    assert page.count('name="set_offered"') == m.Property.query.filter_by(is_active=True).count()
    assert f'name="set_offered" value="{bare.id}"' in page
    assert "Changing heating settings." in page
