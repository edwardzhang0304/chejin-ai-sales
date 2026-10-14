"""Use the same exact shortcode resolver as formal backend admission."""
from .shared_rules import hook_contact_rules as _rules
ShortcodeCandidate = _rules.ShortcodeCandidate
resolve_friend_shortcode = _rules.resolve_friend_shortcode
