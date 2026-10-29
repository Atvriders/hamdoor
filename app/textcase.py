"""Letter case for names and places that arrive in capitals.

The FCC's weekly extract, the callsign provider (callook.info) and HearHam
write names and places in capitals: "ARRL HQ OPERATORS CLUB", "NEWINGTON",
"225 MAIN ST". hamdoor prints them in normal case, but str.title() broke the
words hams write in capitals ("Arrl Hq Operators Club") and street numbers
("1St Ave"). smart_title keeps acronyms, words without a vowel ("HQ", "NWS",
"RR") and Roman numerals in capitals; place_title does the same for a town,
where a word without a vowel is a postal abbreviation instead ("PINE GRV
MLS" -> "Pine Grv Mls"). from_capitals applies them only to text written all
in capitals, so whatever a person typed in mixed case stays theirs. The FCC's
own fields go through tidy_case, which also mends what licensees typed all in
lower case ("william T douglass") or in odd case ("ChARLES", "WRIGHt").
place_key is the spelling-blind form place search matches on.
"""

import re
import unicodedata

# words with a vowel that are still written in capitals (a word without one,
# "HQ", "NWS", "RR 2", stays in capitals anyway): radio and emergency-service
# names, postal and military-mail words, compass points and Roman numerals
_ACRONYMS = frozenset({
    "ARRL", "ARC", "ARA", "ARES", "RACES", "EOC", "USA", "US", "AMSAT", "APRS", "ATV", "CERT", "EME", "FEMA",
    "IRLP", "NASA", "QCWA", "QSO", "UHF", "USAF", "USCG", "USMC", "USN", "YL", "YLRL", "NY", "NYC",
    "PO", "APO", "FPO", "DPO", "AFB", "NE", "SE",
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "XIII", "XIV", "XV", "XVI",
})
# words without a vowel that are abbreviations rather than acronyms: names,
# streets and places ("Jr", "Main St", "Mt Vernon", "Colorado Spgs")
_ABBREVIATIONS = frozenset({
    "JR", "SR", "DR", "MR", "MRS", "MS", "WM", "NG", "MC",
    "ST", "MT", "FT", "PT", "RD", "LN", "PL", "SQ", "BLVD", "PKWY", "HWY", "TPKE", "TRL", "CTR", "PLZ",
    "FWY", "CRK", "SPG", "SPGS", "HTS", "JCT", "MTN", "FLS", "PK", "BCH", "SHRS", "TWP", "CNTY",
})
# a town's, a base's or a military mail address's words that stay in capitals
# (place_title): "USAF Academy", "Eglin AFB", "Joint Base MDL", "March ARB",
# "NE Kirkland", "APO". A radio club's acronym is a word in a town ("DES ARC" ->
# "Des Arc"), and so is any other word with a vowel
_PLACE_ACRONYMS = frozenset({
    "USA", "US", "USAF", "NYC", "DC", "PO", "APO", "FPO", "DPO", "AFB", "ARB", "SFB", "JB", "JBLM", "JBPHH",
    "JBSA", "MCB", "MCBH", "MDL", "GMF", "WSMR", "NE", "NW", "SE", "SW",
})
# the FCC's postal abbreviations of town words: in capitals inside a town a
# licensee wrote in mixed case they are still abbreviations ("Farmington HLS",
# "Incline VLG"), where other vowel-less capitals are the person's acronyms
_PLACE_ABBREVIATIONS = frozenset({
    "GRV", "MLS", "HLS", "VLG", "HTS", "HGTS", "HGHTS", "HGHLNDS", "LK", "LKS", "PLM", "RCH", "RNH", "RDS", "XRDS",
    "HBR", "BRNCH", "BRK", "CLR", "GDNS", "CV", "SN", "PRT", "STN", "MNR", "TWNSHP", "TWSP",
})
# little words that stay lower case inside a name: "Isle of Palms", "Radio Club of America"
_SMALL = frozenset({"OF", "THE", "AND", "AT"})
# little words a person writes in lower case inside a name or a town: tidy_case
# keeps them so in mixed-case text ("Coto de Caza", "Truth or Consequences",
# "Coeur d Alene", "van der Berg"), and at the start of a surname ("de la Cruz")
_PARTICLES = frozenset({
    "of", "the", "and", "or", "at", "by", "on", "in", "upon", "d", "de", "del", "della", "der", "des", "di", "du",
    "da", "das", "do", "dos", "la", "las", "le", "les", "los", "van", "von", "ten", "ter", "op", "el", "al", "bin",
    "ibn",
})
_APOSTROPHES = "'’"
_VOWEL = re.compile(r"[AEIOUY]")
_ORDINAL = re.compile(r"(\d+)(ST|ND|RD|TH)")
# a word: letters and digits (any script), with apostrophes inside ("O'BRIEN");
# hyphens, slashes, dots and spaces separate words ("WINSTON-SALEM", "J.R.", "ST.")
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def _capitalised(word: str) -> str:
    """'o'brien' -> "O'Brien", "john's" -> "John's", 'mckinney' -> 'McKinney'."""
    parts = re.split(r"(['’])", word.lower())
    out = []
    for i, part in enumerate(parts):
        if i % 2 or (i and len(parts[i - 2]) > 1):
            out.append(part)  # an apostrophe, or the "s" of "John's" (but the "Brien" of "O'Brien" is a name)
        elif part.startswith("mc") and len(part) > 3:
            out.append("Mc" + part[2].upper() + part[3:])
        else:
            out.append(part[:1].upper() + part[1:])
    return "".join(out)


def _word_case(word: str, first: bool) -> str:
    up = word.upper()
    if up[0].isdigit():
        m = _ORDINAL.fullmatch(up)
        return m.group(1) + m.group(2).lower() if m else up  # "1st", "4B"
    if any(c.isdigit() for c in up):
        return up  # callsigns and rural addresses: "W1AW", "N1234"
    if up in _ACRONYMS or (up.isascii() and not _VOWEL.search(up) and up not in _ABBREVIATIONS):
        return up
    if up in _SMALL and not first:
        return word.lower()
    return _capitalised(word)


def _place_word_case(word: str, first: bool, last: bool, only: bool, region: str) -> str:
    up = word.upper()
    if any(c.isdigit() for c in up):
        return _word_case(word, first)  # "1st", "4B"
    if up in _PLACE_ACRONYMS or (up == region and last and not first) \
            or (only and up.isascii() and not _VOWEL.search(up)):
        return up  # "USAF", the state after the town ("AUSTIN, TX"), a town written as its initials ("SLC")
    if up in _SMALL and not first:
        return word.lower()
    return _capitalised(word)  # "Grv", "Hghlnds", "Arc"


def _retitle(s: str, case) -> str:
    """s with each word w as case(w, first, last, only): the first word, the
    last word with a letter in it, the only word with a letter in it."""
    s = s or ""
    ms = list(_WORD.finditer(s))
    lettered = [i for i, m in enumerate(ms) if any(c.isalpha() for c in m.group(0))]
    last = lettered[-1] if lettered else -1
    out, end = [], 0
    for i, m in enumerate(ms):
        out += [s[end:m.start()], case(m.group(0), i == 0, i == last, i == last and len(lettered) == 1)]
        end = m.end()
    out.append(s[end:])
    return "".join(out)


def smart_title(s: str) -> str:
    """Title case that keeps acronyms: 'ARRL HQ OPERATORS CLUB' ->
    'ARRL HQ Operators Club', '1ST AVE NE' -> '1st Ave NE', 'ST. LOUIS' ->
    'St. Louis', "O'FALLON" -> "O'Fallon", 'JOHN Q SMITH JR' -> 'John Q Smith Jr'."""
    return _retitle(s, lambda w, first, last, only: _word_case(w, first))


def place_title(s: str, region: str = "") -> str:
    """Title case for a town. A word without a vowel is a postal abbreviation
    ('PINE GRV MLS' -> 'Pine Grv Mls', 'HGHLNDS RANCH' -> 'Hghlnds Ranch') and
    a radio club's acronym is a word ('DES ARC' -> 'Des Arc'); capitals stay
    for _PLACE_ACRONYMS ('USAF ACADEMY' -> 'USAF Academy'), for the
    licence's own state after the town ('AUSTIN, TX' with region 'TX') and
    for a town written as its initials ('SLC')."""
    region = (region or "").upper()
    return _retitle(s, lambda w, first, last, only: _place_word_case(w, first, last, only, region))


def _odd_case(word: str) -> bool:
    """Capitals and small letters mixed the way no name is written: 'ChARLES',
    'WRIGHt', 'HiLLS', 'MIchael', "O'brien". Not 'McDonald', 'DeMeyer',
    'JoAnn', "O'Brien" or 'deJong': there every capital starts a run of small
    letters; nor 'ODonnell', 'OFallon' or 'DAngelo', O' and D' without the apostrophe."""
    if word.isupper() or word.islower():
        return False
    if len(word) > 2 and word[0] in "OD" and word[1].isupper() and word[2].islower():
        word = word[1:]
    for i, c in enumerate(word):
        if not c.isupper():
            continue
        nxt = word[i + 1:i + 2]
        if nxt and nxt in _APOSTROPHES:
            if i == 0 and word[2:3].islower():
                return True  # "O'brien"
        elif not nxt.islower():
            return True
    return False


def _kind(word: str) -> str:
    """'upper', 'lower', 'odd' or 'proper' (any other mix: 'Newington',
    'McDonald', 'WaKeeney'); '' for a word without a letter."""
    if not any(c.isalpha() for c in word):
        return ""
    if word.isupper():
        return "upper"
    if word.islower():
        return "lower"
    return "odd" if _odd_case(word) else "proper"


def _meant_capitals(up: str, kind: str, region: str) -> bool:
    """Whether a word typed in capitals inside mixed-case text is an acronym
    the person meant ('Hawaii ARES', 'Joint Base MDL', 'Austin, TX', 'Joshua
    DM', 'Woods II') rather than shouting or an abbreviation ('Pismo BEACH',
    'EL Paso', 'Mc CORD', 'ST. Joseph', 'Monroe TWP')."""
    if kind == "org":
        return True  # a club's name: "MS Event Support ARC" (Mississippi), "Hawaii ARES"
    if up in _ABBREVIATIONS or (kind == "place" and up in _PLACE_ABBREVIATIONS):
        return False
    if any(c.isdigit() for c in up) or (up.isascii() and not _VOWEL.search(up)):
        return True
    if kind == "place":
        return up in _PLACE_ACRONYMS or up == region
    return len(up) <= 2 or up in _ACRONYMS  # a person's initials, "III"


# acronyms that are never a word or a place name: written in capitals even in
# text that came in mixed case (HearHam lists W1AW's site as "Arrl Headquarters").
# ARES, RACES, ARC and USA stay out: Ares is a town in Spain, Usa one in Japan
_NEVER_WORDS = re.compile(r"\b(?:arrl|hq|eoc|nws)\b", re.I)


def fix_acronyms(s: str) -> str:
    """'Arrl Headquarters' -> 'ARRL Headquarters' (see _NEVER_WORDS)."""
    return _NEVER_WORDS.sub(lambda m: m.group(0).upper(), s or "")


def from_capitals(s: str, place: bool = False) -> str:
    """smart_title(s) (place_title for a town) when s is written in capitals;
    text with a lower-case letter in it (typed by a person) is returned as it is."""
    if not s or not any(c.isalpha() for c in s) or any(c.islower() for c in s):
        return s or ""
    return place_title(s) if place else smart_title(s)


def tidy_case(s: str, kind: str = "name", region: str = "", inner: bool = False) -> str:
    """A field of the FCC's extract in normal case. kind: 'name' (a part of a
    person's name), 'org' (a club's or other licensee's name) or 'place' (a
    town; region: the licence's state).

    Text in capitals or all in lower case is title-cased (place_title for a
    town, else smart_title): 'ARRL HQ OPERATORS CLUB', 'taylorville',
    'tracy orvik', and words in odd case with them ('LIviNGSTON',
    'BLOOMFIELD HiLLS'). In mixed case what the licensee wrote stays, except
    words all in lower case ('Pleasant view' -> 'Pleasant View', 'John
    kenneth'; a particle inside stays: 'Coto de Caza', 'van der Berg'), words
    in odd case ('Signal HIll' -> 'Signal Hill', "O'brien") and words in
    capitals that are not acronyms (_meant_capitals); a town also starts with
    a capital ('deQuincy' -> 'DeQuincy'). A club's name keeps its capitals and
    its stylings ('WPI LnL Radio Club', 'HouBEARS'): only its words all in
    lower case change. inner: the field goes on a name (a middle initial, a
    surname), so it may start with a particle ('de la Cruz')."""
    if not s:
        return ""
    region = (region or "").upper()
    place = kind == "place"
    ms = list(_WORD.finditer(s))
    kinds = [_kind(m.group(0)) for m in ms]
    written = ("proper", "odd") if kind == "org" else ("proper",)  # a person's own casing
    if not any(k in written for k in kinds):
        if not any(kinds):
            return s
        return place_title(s, region) if place else smart_title(s)
    lettered = [i for i, k in enumerate(kinds) if k]
    out, end = [], 0
    for i, m in enumerate(ms):
        w, k = m.group(0), kinds[i]
        first, last = i == lettered[0], i == lettered[-1]
        if k == "lower" and w in _PARTICLES and not (first and (place or not inner)) \
                and not (place and last and w.upper() == region):
            pass  # "Coeur d Alene", "van der Berg"; but "Siletz,or" is in Oregon
        elif k == "lower" or (k == "odd" and kind != "org"):
            w = _place_word_case(w, first, last, False, region) if place else _word_case(w, first and not inner)
        elif k == "upper" and not _meant_capitals(w.upper(), kind, region):
            w = _capitalised(w)
        elif k == "proper" and place and first and w[0].islower():
            w = w[0].upper() + w[1:]
        out += [s[end:m.start()], w]
        end = m.end()
    out.append(s[end:])
    return "".join(out)


def place_key(s: str) -> str:
    """The form place search compares, on the stored name and on what is
    typed: lower case, accents dropped, no spaces, apostrophes, hyphens or
    periods. "Lee's Summit" and GeoNames' "Lees Summit" -> "leessummit",
    "Winston Salem" and "Winston-Salem" -> "winstonsalem", "McLean" and
    "Mc Lean" -> "mclean", "Mayagüez" -> "mayaguez"."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "").lower() if c.isalnum())
