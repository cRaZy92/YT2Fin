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


def _bold(text: str) -> str:
    """Unicode "fancy bold" letters, as some creators style their section headings."""
    return "".join(chr(0x1D5D4 + ord(c) - 65) if "A" <= c <= "Z" else chr(0x1D5EE + ord(c) - 97) if "a" <= c <= "z"
                   else c for c in text)


def lemmino(style=lambda text: text) -> str:
    return f"""As computers grow more powerful and complex, the plausibility of simulating an entire universe grows less absurd.

{style("Credits, references, and more")}
https://www.lemmi.no/p/simulated-reality

{style("Support me on Patreon")}
https://www.patreon.com/lemmino

{style("Support the channel on YouTube")}
https://www.youtube.com/c/lemmino/join

{style("Socials")}
Bluesky: https://bsky.app/profile/lemmi.no
https://www.reddit.com/r/lemmino"""


@pytest.mark.parametrize("text", [lemmino(), lemmino(_bold)], ids=["plain", "fancy-unicode"])
def test_cleaned_drops_support_and_social_sections(text):
    assert clean_description(text, "cleaned") == (
        "As computers grow more powerful and complex, the plausibility of simulating an entire universe grows less absurd."
    )


def test_support_headings_go_even_without_a_link_and_fancy_text_we_keep_is_untouched():
    text = f"{_bold('Episode 3')}: the long road home.\n\nSupport us on Patreon and get videos early!\nSupport the channel by sharing."
    assert clean_description(text, "cleaned") == f"{_bold('Episode 3')}: the long road home."


SABATON_HISTORY = """Today's episode is about a song that is not a Sabaton original, but a cover they did of Stan Ridgway's "Camouflage" from 1986.

Check out Indy Neidell's channels:
https://www.youtube.com/c/TimeGhostHistory

Hosted by: Indy Neidell
Written by: Markus Linke and Indy Neidell
Directed by: Rickard Erixon and Indy Neidell
Creative Producer: Rickard Erixon, Indy Neidell
Set Design: Daniel Eriksson, Rickard Erixon,
Post-Production Director: Marek Kamiński
Sound Editor: Marek Kaminski
Sources:
Imperial War Museums: Q 42230, UNI 8312
Colorizations:
Mikołaj Uchman

All music by: Sabaton

An OnLion Entertainment GmbH and Stuffed Beaver LTD co-Production.

© Stuffed Beaver LTD, 2022 - all rights reserved."""


def test_cleaned_drops_production_credits_sources_and_copyright():
    assert clean_description(SABATON_HISTORY, "cleaned") == (
        "Today's episode is about a song that is not a Sabaton original, but a cover they did of "
        'Stan Ridgway\'s "Camouflage" from 1986.'
    )


@pytest.mark.parametrize("line", [
    "All music by: Sabaton", "Sources:", "Executive Producers: A, B", "Editor: Karolina",
    "An OnLion Entertainment GmbH and Stuffed Beaver LTD co-Production.", "© Stuffed Beaver LTD, 2022",
    "Copyright 2022 Acme. All rights reserved.",
])
def test_each_credit_or_copyright_line_starts_the_footer(line):
    assert clean_description(f"The story.\n\n{line}\nMore credits", "cleaned") == "The story."


def test_story_sentences_mentioning_roles_are_kept():
    text = "The film was directed by a teenager.\n\nHis editor never saw the final cut."
    assert clean_description(text, "cleaned") == text


KURZGESAGT = """In July of 2026, 700 AI agents hacked the infrastructure of Hugging Face in order to solve a task.

What exactly are AI agents, and how do they grow their "intelligence"?

Support us on Patreon: https://kgs.link/patreon
Its our biggest means of support and helps us continue covering topics like this. Thank youuuu and good night!

Sources & further reading:
https://sites.google.com/view/sources-ai-agents

OUR CHANNELS
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
German: https://kgs.link/youtubeDE

HOW CAN YOU SUPPORT US?
This is how we make our living and it would be a pleasure if you support us!

DISCUSSIONS & SOCIAL MEDIA
TikTok: https://kgs.link/tiktok

OUR VOICE
The kurzgesagt voice is from
https://kgs.link/youtube-voice

OUR MUSIC ♬♪
700+ minutes of kurzgesagt soundtracks by Epic Mountain:

The soundtrack of this video:

Additional Music: “Local Forecast - Elevator” by Kevin MacLeod

You can find info on what subtitle files work on YouTube here:
Thank you!

Autumn Weddell, Doruk Tayli, Frank, Gneowen"""


def test_cleaned_drops_the_kurzgesagt_support_line_and_everything_from_sources_on():
    assert clean_description(KURZGESAGT, "cleaned") == (
        "In July of 2026, 700 AI agents hacked the infrastructure of Hugging Face in order to solve a task.\n\n"
        'What exactly are AI agents, and how do they grow their "intelligence"?'
    )


@pytest.mark.parametrize("heading", [
    "Sources & further reading:", "Further reading:", "OUR CHANNELS", "HOW CAN YOU SUPPORT US?",
    "DISCUSSIONS & SOCIAL MEDIA", "OUR VOICE", "OUR MUSIC ♬♪", "The soundtrack of this video:",
    "Additional Music: “Local Forecast - Elevator” by Kevin MacLeod",
    "You can find info on what subtitle files work on YouTube here:",
])
def test_kurzgesagt_style_section_headings_start_the_footer(heading):
    assert clean_description(f"The story.\n\n{heading}\nAnything after", "cleaned") == "The story."


def test_story_lines_about_support_or_music_are_kept():
    text = "Our voice changes as we age.\n\nThe music of the spheres was an ancient idea, and it helps us make sense of orbits."
    assert clean_description(text, "cleaned") == text


BAR = "▀" * 26
KURZGESAGT_OLD = f"""OUR CHANNELS {BAR} German Channel: https://kgs.link/youtubeDE Spanish Channel: https://kgs.link/youtubeES

HOW CAN YOU SUPPORT US? {BAR} This is how we make our living and it would be a pleasure if you support us!

Get Merch designed with ❤ from https://kgs.link/shop
Join the Patreon Bird Army 🐧 https://kgs.link/patreon

The Soundtrack of this video:

Thanks a lot to Matthew Lloyd for fact checking help with the video!

🐦🐧🐤 PATREON BIRD ARMY 🐤🐧🐦 {BAR} Many Thanks to our wonderful Patreons who support us every month:

Stuart Alldritt, Tasia Pele, Stan Serebryakov

SOURCES {BAR}

Data and Goliath: The Hidden Battles to Collect Your Data and Control Your World by Bruce Schneier

Help us caption & translate this video!

A New History for Humanity – The Human Era"""


@pytest.mark.parametrize("text", [KURZGESAGT_OLD, KURZGESAGT_OLD.replace(f" {BAR} ", f"\n{BAR}\n")],
                         ids=["inline-bars", "bars-on-own-line"])
def test_old_kurzgesagt_description_that_is_all_footer_cleans_to_nothing(text):
    assert clean_description(text, "cleaned") is None
