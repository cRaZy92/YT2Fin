import pytest

from app.descriptions import clean_description

STORYTIME = """In this video we look at 10 photos that seem normal at first, until you learn the story behind them.

Some of these cases were never solved, and a few of them still keep investigators up at night.

🔔 Subscribe for more: https://youtube.com/@ewustorytime?sub_confirmation=1
👕 Get our merch: https://ewu.store
This video is sponsored by NordVPN! Use code EWU for 70% off: https://nordvpn.com/ewu

Timestamps:
0:00 Intro
1:32 Photo 1
14:05 Photo 10

▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
Follow us:
Instagram: https://instagram.com/ewu
Business inquiries: ewu@example.com
#truecrime #storytime #mystery"""

SPONSOR_FIRST = """Get 20% off Brilliant with my link https://brilliant.org/x

Why do mirrors flip left and right but not up and down? We dig into the geometry.

Music by Epidemic Sound: https://epidemicsound.com"""


def test_cleaned_keeps_the_story_and_drops_promos_links_chapters_and_footer():
    out = clean_description(STORYTIME, "cleaned")
    assert out == (
        "In this video we look at 10 photos that seem normal at first, until you learn the story behind them.\n\n"
        "Some of these cases were never solved, and a few of them still keep investigators up at night."
    )


def test_first_paragraph_skips_a_sponsor_block_at_the_top():
    assert clean_description(SPONSOR_FIRST, "first_paragraph") == (
        "Why do mirrors flip left and right but not up and down? We dig into the geometry."
    )
    assert clean_description(STORYTIME, "first_paragraph").startswith("In this video we look at 10 photos")
    assert "Some of these cases" not in clean_description(STORYTIME, "first_paragraph")


def test_full_and_none_modes():
    assert clean_description(STORYTIME, "full") == STORYTIME
    assert clean_description(STORYTIME, "none") is None


def test_extra_terms_remove_matching_lines_case_insensitively():
    text = "A real summary.\nThanks to RAID Shadow Legends for supporting the channel."
    assert clean_description(text, "cleaned", ["raid shadow legends"]) == "A real summary."


@pytest.mark.parametrize("text", [None, "", "https://only-a-link.example", "#tag #only"])
def test_nothing_left_means_no_description(text):
    assert clean_description(text, "cleaned") is None


def test_unknown_mode_falls_back_to_cleaned():
    assert clean_description("Story.\nhttps://x.example", "weird") == "Story."


def test_partner_block_and_disclosures_end_the_description():
    text = ("We benchmark the new laptop chip in games and apps.\n\n"
            "Check out our Channel Partners:\n\nSecretlab\n- Grab a chair: https://lmg.gg/x\n\nPIA\n\n"
            "Linus Sebastian is an investor in Framework Computer, Inc.")
    assert clean_description(text, "cleaned") == "We benchmark the new laptop chip in games and apps."


def test_lines_without_letters_or_digits_are_dropped():
    assert clean_description("New smart home display.\n\n~\n\n***", "cleaned") == "New smart home display."


def test_stock_footage_credit_lines_are_dropped():
    text = "We cover 3 photos with disturbing backstories...\n\n[Video] [Imagery] supplied by Getty Images."
    assert clean_description(text, "cleaned") == "We cover 3 photos with disturbing backstories..."
