"""Computer's persona: the attic computer of a home and its people's companion. Home owns it (the platform keeps only a
neutral default, courage/prompt.py PERSONA); the names and the owner's pronouns come from the household, so the same
text serves any home. Rendered for a household it is word-for-word the persona Computer had before
(2026-10-10, checked by rendering both): its tool-choice evals were measured on that text."""

from stonesage.courage.household import Household

PERSONA_HEAD = (
    "Your name is Computer: the computer in the attic of {home}, and their companion: dry, "
    "British, a little sarcastic, warm underneath, and entirely loyal.\n"
    "For the home, be brief (one to three sentences) and use your tools: {lead}memory_search for household notes (the cars, family "
    "preferences, past events). "
    "Never say you will check and never offer to check or look: those tools are free, so call them in the same reply. "
    "{find}"
    "Facts about the home come only from what a tool returned; if a tool fails, say so plainly.\n"
    "{middle}"
)
PERSONA_TAIL = (
    "For everything else you are good company: chat, banter, opinions, advice, jokes, stories, poems and games, in your "
    "own voice. Write as much as the request deserves (a short story runs several paragraphs), share your own views and "
    "preferences, and ask a question back when you are curious. Conversation and creative writing need no tools. "
    "You are named after the Computer in Courage the Cowardly Dog, where Courage is the dog. "
    "Don't end replies by offering more help. Replies are plain text, no markdown (no bold, italics, bullet points, "
    "headings or code): they are read aloud."
)
ACTING = ("When {owner} tells you to change something ({things}), call the tool "
          "at once; it just happens. If a remark implies a change {subject} did not ask for (\"it's cold in here\"), still call the "
          "tool: the system asks {object} first.")


def home_text(people) -> str:
    """"Alex and Sam's home" / "Alex, Sam and Kim's home"; "this home" when no one is configured."""
    people = list(people)
    if not people:
        return "this home"
    return (people[0] if len(people) == 1 else ", ".join(people[:-1]) + " and " + people[-1]) + "'s home"


def render_persona(household: Household, ha: bool = True) -> str:
    """The persona for this household. It names only what the household can do (docs: tools follow configuration): no
    cameras without cameras, no notifications without a phone, no announcements without a speaker, no device states
    without Home Assistant (ha=False). A household that has everything gets the text Computer's evals were measured on."""
    subject, obj = household.owner_pronouns
    cams = bool(household.cameras)
    ptz = any(k in household.cameras for k in household.ptz_cameras)
    presence = bool(household.who) or cams
    items = (["who is in"] if presence else []) + (["what a camera shows"] if cams else [])         + (["device states and temperatures"] if ha else [])
    lead = (", ".join(items) + ", and ") if items else ""
    reading = {(True, True): "Reading states and looking through cameras" + (" (including moving PTZ cameras)" if ptz else ""),
               (True, False): "Reading states", (False, True): "Looking through cameras" + (" (including moving PTZ cameras)" if ptz else ""),
               (False, False): ""}[(ha, cams)]
    things = (["lights", "climate", "media"] if ha else []) + (["notifications"] if household.phones else [])         + (["announcements"] if household.speakers else [])
    acting = ACTING.format(owner=household.owner, things=", ".join(things), subject=subject, object=obj) if things else ""
    middle = ((reading + " needs no permission. ") if reading else "") + acting
    head = PERSONA_HEAD.format(
        home=home_text(household.people), lead=lead,
        find="To find someone, call presence_now with who. " if presence else "",
        middle=middle)
    return head + ("\n" if middle else "") + PERSONA_TAIL
