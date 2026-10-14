"""Home: the consumer app on top of the StoneSage platform (docs/LAYERS.md, layer 2).
Home Assistant, cameras, presence, voice UX, Alexa. It may import the platform and the foundation; they never import it.
It reaches them by registering tools, routes and reflexes (see hearth/ha_tools.py), not by being imported."""

import stonesage as _stonesage
from lodestone import API_VERSION as _LODESTONE_API
from lodestone import compat as _compat

__version__ = "0.1.0rc5"
STONESAGE_API = "0.1"    # the StoneSage API this version is written for (stonesage.API_VERSION), checked at import
LODESTONE_API = "0.1"    # the Lodestone API this version is written for, checked at import

_compat.require("Hearth", __version__, "StoneSage", _stonesage.API_VERSION, STONESAGE_API)
_compat.require("Hearth", __version__, "Lodestone", _LODESTONE_API, LODESTONE_API)
