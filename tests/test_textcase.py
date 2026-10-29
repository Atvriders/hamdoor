"""Acronym-safe title case for names and places that arrive in capitals (QA F6, F7)."""

import pytest

from app.textcase import fix_acronyms, from_capitals, place_title, smart_title, tidy_case


@pytest.mark.parametrize("raw, cased", [
    # QA F7: the FCC's and HearHam's capitals
    ("ARRL HQ OPERATORS CLUB", "ARRL HQ Operators Club"),
    ("ARRL HEADQUARTERS", "ARRL Headquarters"),
    ("KANSAS CITY ARES / RACES", "Kansas City ARES / RACES"),
    ("NEWINGTON EOC", "Newington EOC"),
    ("RADIO CLUB OF THE USA", "Radio Club of the USA"),
    ("CT VALLEY ARC", "CT Valley ARC"),          # no vowel: an acronym
    ("NWS SKYWARN NET", "NWS Skywarn Net"),
    ("YL CLUB", "YL Club"),
    # names
    ("JOHN Q SMITH JR", "John Q Smith Jr"),
    ("JOHN SMITH III", "John Smith III"),
    ("O'BRIEN", "O'Brien"),
    ("D'IBERVILLE", "D'Iberville"),
    ("ST JOHN'S ARC", "St John's ARC"),
    ("MCKINNEY", "McKinney"),
    ("J.R. EWING", "J.R. Ewing"),
    ("NG", "Ng"),
    ("LYNN", "Lynn"),                            # Y counts as a vowel
    ("TY COBB", "Ty Cobb"),
    # streets and places
    ("225 MAIN ST", "225 Main St"),
    ("1ST AVE NE", "1st Ave NE"),                # str.title() made "1St Ave Ne"
    ("102ND ST SW", "102nd St SW"),
    ("APT 4B", "Apt 4B"),
    ("PO BOX 12", "PO Box 12"),
    ("P.O. BOX 132", "P.O. Box 132"),
    ("RR 2 BOX 30", "RR 2 Box 30"),
    ("N1234 COUNTY RD K", "N1234 County Rd K"),
    ("ST. LOUIS", "St. Louis"),
    ("MT VERNON", "Mt Vernon"),
    ("FT WORTH", "Ft Worth"),
    ("COLORADO SPGS", "Colorado Spgs"),
    ("WINSTON-SALEM", "Winston-Salem"),
    ("ISLE OF PALMS", "Isle of Palms"),
    ("THE VILLAGES", "The Villages"),
    ("MARS HILL", "Mars Hill"),
    ("EGLIN AFB", "Eglin AFB"),
    ("APO", "APO"),
    ("QUÉBEC", "Québec"),
    ("W1AW", "W1AW"),
    ("kansas city", "Kansas City"),
    ("", ""),
])
def test_smart_title(raw, cased):
    assert smart_title(raw) == cased
    assert smart_title(cased) == cased  # stable: a second pass changes nothing


def test_from_capitals_leaves_what_a_person_typed():
    assert from_capitals("NEWINGTON") == "Newington"
    assert from_capitals("Newington") == "Newington"
    assert from_capitals("John McDonald") == "John McDonald"   # .title() made "Mcdonald"
    assert from_capitals("deKalb") == "deKalb"
    assert from_capitals("06111") == "06111" and from_capitals("") == ""


def test_acronyms_that_are_never_words_are_fixed_in_mixed_case_text():
    assert fix_acronyms("Arrl Headquarters") == "ARRL Headquarters"
    assert fix_acronyms("Newington Eoc") == "Newington EOC"
    assert fix_acronyms("Ares") == "Ares" and fix_acronyms("Usa") == "Usa"  # towns in Spain and Japan
    assert fix_acronyms("Harlequin") == "Harlequin"  # whole words only


# ---------------------------------------------------------------- QA R1: towns, lower case and odd case

@pytest.mark.parametrize("raw, region, cased", [
    # the FCC's postal abbreviations have no vowel, and they are not acronyms in a town
    ("PINE GRV MLS", "PA", "Pine Grv Mls"),
    ("EL DORADO HLS", "CA", "El Dorado Hls"),
    ("HOT SPRINGS VLG", "AR", "Hot Springs Vlg"),
    ("HGHLNDS RANCH", "CO", "Hghlnds Ranch"),
    ("N RICHLND HLS", "TX", "N Richlnd Hls"),
    ("COLORADO SPGS", "CO", "Colorado Spgs"),
    ("DES ARC", "AR", "Des Arc"),             # a radio club's acronym is a word here
    ("BOIS D ARC", "MO", "Bois D Arc"),
    # what a town keeps in capitals
    ("USAF ACADEMY", "CO", "USAF Academy"),
    ("EGLIN AFB", "FL", "Eglin AFB"),
    ("JOINT BASE MDL", "NJ", "Joint Base MDL"),
    ("NE KIRKLAND", "WA", "NE Kirkland"),
    ("APO", "AE", "APO"),
    ("SLC", "UT", "SLC"),                     # a town written as its initials
    ("AUSTIN, TX", "TX", "Austin, TX"),       # the licence's own state after the town
    ("LA PLACE", "LA", "La Place"),           # ... but not before it
    # and the rest as smart_title
    ("MC KINNEY", "TX", "Mc Kinney"),
    ("MCKINNEY", "TX", "McKinney"),
    ("ST. LOUIS", "MO", "St. Louis"),
    ("ISLE OF PALMS", "SC", "Isle of Palms"),
    ("O'FALLON", "MO", "O'Fallon"),
    ("NEWINGTON", "CT", "Newington"),
    ("taylorville", "IL", "Taylorville"),
    ("new york", "NY", "New York"),
    ("12345", "", "12345"),
    ("", "", ""),
])
def test_place_title(raw, region, cased):
    assert place_title(raw, region) == cased
    assert place_title(cased, region) == cased  # stable


@pytest.mark.parametrize("raw, kind, region, inner, cased", [
    # all in lower case: title-cased like capitals (str.title() did, the merged rule did not)
    ("taylorville", "place", "IL", False, "Taylorville"),
    ("madison hts", "place", "MI", False, "Madison Hts"),
    ("william", "name", "", False, "William"),
    ("douglass", "name", "", True, "Douglass"),
    ("tracy orvik", "name", "", False, "Tracy Orvik"),
    ("mclean", "name", "", True, "McLean"),
    ("kansas city ares", "org", "", False, "Kansas City ARES"),
    # odd case: as if it were capitals
    ("ChARLES", "name", "", False, "Charles"),
    ("WRIGHt", "name", "", True, "Wright"),
    ("MIchael", "name", "", False, "Michael"),
    ("McKINNEY", "place", "TX", False, "McKinney"),
    ("BLOOMFIELD HiLLS", "place", "MI", False, "Bloomfield Hills"),
    ("Signal HIll", "place", "CA", False, "Signal Hill"),
    ("O'brien", "name", "", True, "O'Brien"),
    # mixed case: kept, except words all in lower case ...
    ("Pleasant view", "place", "UT", False, "Pleasant View"),
    ("John kenneth", "name", "", False, "John Kenneth"),
    ("Al-francis", "name", "", False, "Al-Francis"),
    ("mc Coy", "name", "", True, "Mc Coy"),
    ("o Connor", "name", "", True, "O Connor"),
    ("Radio Club of america", "org", "", False, "Radio Club of America"),
    ("Anna, tx", "place", "TX", False, "Anna, TX"),
    ("Siletz,or", "place", "OR", False, "Siletz,OR"),
    # ... but particles inside, and at the start of a surname
    ("de Steiguer", "name", "", True, "de Steiguer"),
    ("van der Berg", "name", "", True, "van der Berg"),
    ("Coto de Caza", "place", "CA", False, "Coto de Caza"),
    ("Truth or Consequences", "place", "NM", False, "Truth or Consequences"),
    ("Coeur d Alene", "place", "ID", False, "Coeur d Alene"),
    # ... words in capitals that are not acronyms
    ("Pismo BEACH", "place", "CA", False, "Pismo Beach"),
    ("EL Paso", "place", "TX", False, "El Paso"),
    ("ST. Joseph", "place", "MO", False, "St. Joseph"),
    ("Monroe TWP", "place", "NJ", False, "Monroe Twp"),
    ("Farmington HLS", "place", "MI", False, "Farmington Hls"),  # a postal abbreviation, not an acronym
    ("Incline VLG", "place", "NV", False, "Incline Vlg"),
    ("Mc CORD", "name", "", True, "Mc Cord"),
    # ... and a town starts with a capital
    ("deQuincy", "place", "LA", False, "DeQuincy"),
    # what a person meant stays
    ("McDonald", "name", "", True, "McDonald"),
    ("DeMeyer", "name", "", True, "DeMeyer"),
    ("JoAnn", "name", "", False, "JoAnn"),
    ("deJong", "name", "", True, "deJong"),
    ("ODonnell", "name", "", True, "ODonnell"),   # O' and D' without the apostrophe
    ("DAngelo", "name", "", True, "DAngelo"),
    ("OFallon", "place", "MO", False, "OFallon"),
    ("WaKeeney", "place", "KS", False, "WaKeeney"),
    ("Joshua DM", "name", "", False, "Joshua DM"),
    ("Woods II", "name", "", True, "Woods II"),
    ("Joint Base MDL", "place", "NJ", False, "Joint Base MDL"),
    ("Eglin AFB", "place", "FL", False, "Eglin AFB"),
    ("CA  Moreno Valley", "place", "CA", False, "CA  Moreno Valley"),
    ("SEMA EmComm", "org", "", False, "SEMA EmComm"),
    ("WPI LnL Radio Club", "org", "", False, "WPI LnL Radio Club"),  # a club's own styling
    ("MS Event Support Amateur Radio Club", "org", "", False, "MS Event Support Amateur Radio Club"),
    ("ARRL HQ Operators Club", "org", "", False, "ARRL HQ Operators Club"),
    ("", "place", "", False, ""),
])
def test_tidy_case(raw, kind, region, inner, cased):
    assert tidy_case(raw, kind, region, inner) == cased
    assert tidy_case(cased, kind, region, inner) == cased  # stable


def test_the_callsign_providers_capitals_follow_the_same_rules():
    """Sign-up and Update from FCC store callook's capitals in normal case; its
    town as the FCC directory prints the same town (QA R1)."""
    from app.integrations.callook import CallookProvider

    rec = CallookProvider._parse("KC3UAS", {
        "status": "VALID", "name": "ERIK LENZING", "location": {}, "current": {"operClass": "GENERAL"},
        "otherInfo": {}, "address": {"line1": "4 ELM ST", "line2": "PINE GRV MLS, PA 16868"}})
    assert (rec.name, rec.address_line, rec.city, rec.state, rec.zip) == (
        "Erik Lenzing", "4 Elm St", "Pine Grv Mls", "PA", "16868")
    assert from_capitals("PINE GRV MLS", place=True) == "Pine Grv Mls"
    assert from_capitals("Pine GRV Mls", place=True) == "Pine GRV Mls"  # typed by a member: theirs
