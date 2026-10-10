"""The household moved out of code (docs/LAYERS.md M2): Home reads it from config.json, the platform's tools and prompt
only render what they are given. These pin the parts a fresh install depends on."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, ROOT)

from stonesage.courage import CourageDeps, CourageTools  # noqa: E402
from stonesage.courage.household import Household  # noqa: E402
from stonesage.courage.prompt import PERSONA, build_presence_card  # noqa: E402
from hearth.household import camera_key, household_from_config, phone_services, speaker_services  # noqa: E402
from hearth.persona import home_text, render_persona  # noqa: E402

NONE = dict(ha_states=None, ha_call=None, presence=lambda: {}, camera_look=None, camera_scan=None)

CFG = {
    "household": {"people": ["Kim", "Max"], "owner_pronouns": ["she", "her"], "pets": {"Pip": "cat"},
                  "phones": {"kim": "mobile_app_kim_phone"},
                  "speakers": {"den": "alexa_media_den", "everywhere": "alexa_media_everywhere"}},
    "camera_ui": {"camera.porch_hd_stream_direct": {"name": "Porch", "ptz": "porch"},
                  "camera.garage": {"name": "Garage"}},
    "frigate": {"identities": {"dog": "moss"}},
}


class TestFromConfig(unittest.TestCase):
    def test_block_and_camera_discovery(self):
        hh = household_from_config(CFG)
        self.assertEqual(hh.people, ["Kim", "Max"])
        self.assertEqual(hh.owner_pronouns, ("she", "her"))
        self.assertEqual(hh.pets, {"Pip": "cat"})                     # the block wins over frigate.identities
        self.assertEqual(hh.cameras, {"porch": {"entity": "camera.porch_hd_stream_direct", "name": "Porch"},
                                      "garage": {"entity": "camera.garage", "name": "Garage"}})
        self.assertEqual(hh.ptz_cameras, ["porch"])
        self.assertEqual(hh.default_camera, "porch")
        self.assertEqual((hh.phones, hh.speakers), (["kim"], ["den", "everywhere"]))
        self.assertEqual(phone_services(CFG), {"kim": "mobile_app_kim_phone"})
        self.assertEqual(speaker_services(CFG)["den"], "alexa_media_den")

    def test_no_block_still_finds_cameras_and_pets(self):
        hh = household_from_config({k: v for k, v in CFG.items() if k != "household"})
        self.assertEqual((hh.people, hh.pets, hh.phones), ([], {"Moss": "dog"}, []))
        self.assertEqual(list(hh.cameras), ["porch", "garage"])
        self.assertEqual(hh.owner_pronouns, ("they", "them"))

    def test_bad_pronouns_fall_back(self):
        self.assertEqual(household_from_config({"household": {"owner_pronouns": ["he"]}}).owner_pronouns, ("they", "them"))

    def test_camera_key(self):
        self.assertEqual(camera_key("camera.driveway_front_door_hd_stream_direct"), "driveway_front_door")
        self.assertEqual(camera_key("camera.kitchen_living_room_hd_stream"), "kitchen_living_room")
        self.assertEqual(camera_key("camera.garage"), "garage")


class TestPersona(unittest.TestCase):
    def test_names_and_pronouns(self):
        p = render_persona(household_from_config(CFG))
        self.assertIn("the attic of Kim and Max's home", p)
        self.assertIn("When Kim tells you to change something", p)
        self.assertIn("a change she did not ask for", p)
        self.assertIn("the system asks her first", p)

    def test_home_text(self):
        self.assertEqual(home_text([]), "this home")
        self.assertEqual(home_text(["Kim"]), "Kim's home")
        self.assertEqual(home_text(["A", "B", "C"]), "A, B and C's home")

    def test_platform_default_names_no_one(self):
        self.assertNotIn("Computer", PERSONA)
        self.assertNotIn("attic", PERSONA)


class TestToolsRenderTheHousehold(unittest.TestCase):
    def test_empty_household_offers_no_names(self):
        tools = CourageTools(CourageDeps(**NONE))
        specs = tools.specs
        self.assertEqual(specs["presence_now"]["parameters"]["properties"]["who"]["enum"], [])
        self.assertEqual(specs["camera_look"]["parameters"]["properties"]["camera"]["enum"], [])
        self.assertIn("the owner approves it first", specs["ha_call"]["description"])
        self.assertIn("'that wasn't Rex, it was Bella'", specs["correct_sighting"]["description"])   # stand-in names
        self.assertIsNotNone(tools.validate("camera_look", {"camera": "kitchen"}))     # no camera to look through

    def test_another_household(self):
        tools = CourageTools(CourageDeps(**NONE, household=household_from_config(CFG)))
        specs = tools.specs
        self.assertIn("people (Kim, Max) and pets (Pip the cat)", specs["presence_now"]["description"])
        self.assertEqual(specs["presence_now"]["parameters"]["properties"]["who"]["enum"], ["kim", "max", "pip"])
        self.assertIn("Kim's phone (the only phone", specs["notify"]["description"])
        self.assertIn("(den or every Echo)", specs["speak"]["description"])
        self.assertIn("Kim approves it first", specs["ha_call"]["description"])
        self.assertEqual(tools._camera_key("Garage"), "garage")
        self.assertEqual(tools._camera_key(None), "porch")                              # the default camera
        self.assertEqual(tools._default_phone(), "kim")

    def test_card_lists_only_who_it_is_given(self):
        state = {"locations": {"pip": {"minutes_ago": 5.0, "camera": "Porch"}, "zed": {"minutes_ago": 1.0}}}
        card = build_presence_card(state, who=["Pip"])
        self.assertIn("- Pip: seen 5 min ago on Porch", card)
        self.assertNotIn("Zed", card)
        self.assertEqual(build_presence_card(state).count("\n"), 0)                    # no household: header only

    def test_lookout_names_who_to_look_for(self):
        hh = Household(people=["Kim"], pets={"Pip": "cat"}, descriptions={"Pip": "grey tabby cat"})
        self.assertEqual(hh.lookout_text(), "Kim, Pip (grey tabby cat)")       # undescribed names still listed
        self.assertEqual(Household().lookout_text(), "")
        hh = household_from_config({"household": {"people": ["Kim"], "descriptions": {"Kim": "tall"}}})
        self.assertEqual(hh.lookout_text(), "Kim (tall)")

    def test_household_who_order(self):
        hh = Household(people=["A"], pets={"P": "cat"})
        self.assertEqual((hh.who, hh.keys, hh.owner), (["A", "P"], ["a", "p"], "A"))


class TestHubFallback(unittest.TestCase):
    """The harness's presence hub falls back to config.json's household when VM 102's profiles can't be read."""

    def test_entities_from_config(self):
        from harness.core.home_presence_hub import _household_entities
        ents = _household_entities({"household": {"people": ["Kim"], "pets": {"Pip": "cat"},
                                                  "descriptions": {"Pip": "grey tabby"}}})
        self.assertEqual([p["name"] for p in ents["people"]], ["Kim"])
        self.assertEqual(ents["pets"][0], {"id": "pet-pip", "name": "Pip", "species": "cat", "traits": "grey tabby",
                                           "alert_level": "trusted_pet"})
        self.assertEqual(_household_entities({}), {"people": [], "pets": []})



class TestInfraWords(unittest.TestCase):
    """Plugs that power the site's own servers are never switched off; the site names its machines in config.json."""

    def test_generic_words_by_default_and_the_sites_own_from_config(self):
        from stonesage.courage.tools import INFRA_WORDS, infra_words
        self.assertEqual(infra_words(), INFRA_WORDS)
        self.assertEqual(household_from_config({}).infra_words, [])
        hh = household_from_config({"infra_words": ["Rack1", " ", "pxe"]})
        self.assertEqual(hh.infra_words, ["Rack1", " ", "pxe"])
        self.assertEqual(infra_words(hh.infra_words), INFRA_WORDS + ("rack1", "pxe"))

    def test_the_loop_refuses_to_turn_off_a_plug_with_one_of_them(self):
        hh = household_from_config({"infra_words": ["rack1"]})
        tools = CourageTools(CourageDeps(**NONE, household=hh))
        for eid, refused in (("switch.rack1_plug", True), ("switch.nas_plug", True), ("switch.desk_lamp", False)):
            err = tools.validate("ha_call", {"domain": "switch", "service": "turn_off", "entity_id": eid}) or ""
            self.assertEqual("powers server infrastructure" in err, refused, (eid, err))
        plain = CourageTools(CourageDeps(**NONE))                      # no config: rack1 is just a plug
        err = plain.validate("ha_call", {"domain": "switch", "service": "turn_off", "entity_id": "switch.rack1_plug"}) or ""
        self.assertNotIn("powers server infrastructure", err)


if __name__ == "__main__":
    unittest.main()
