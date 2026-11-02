"""The meme formats the page uses, and where every word goes on each.

Taking Imgflip's 40 most-captioned templates on trust gave the model formats
it half knew and boxes it could not see: "Two guys on a bus" and "Sleeping
Shaq" were used without their meaning, and Drake once preferred the SSH
backdoor. Each format here was looked at with a measuring grid and carries:

  * where each box sits on the image, as percentages, so the text lands on
    the right person, sign or panel -- drawn by us, not by Imgflip;
  * what each box means and how the joke works, in the words the model reads;
  * a worked developer example, which teaches the format better than rules.

A format joins this list only after its boxes have been drawn and looked at.
The blank images come from Imgflip's own image host, the same files its
editor uses.
"""
from __future__ import annotations

from dataclasses import dataclass

OUTLINE = "outline"     # white meme capitals with a black edge, over a photo
DARK = "dark"           # black sentence case, on a white panel


@dataclass(frozen=True)
class Box:
    x: float            # left edge, percent of the image width
    y: float            # top edge, percent of the image height
    w: float
    h: float
    role: str           # what goes here, as the model is told it
    style: str = OUTLINE
    max_chars: int = 40
    max_px: int | None = None   # a lower ceiling on text size, where the whole box is one small board


@dataclass(frozen=True)
class Format:
    id: str             # Imgflip's template id: the blank image, and the editor link
    name: str
    image: str          # the blank template on i.imgflip.com
    width: int
    height: int
    how: str            # how the joke works
    boxes: tuple[Box, ...]
    example: tuple[str, ...]

    @property
    def maker(self) -> str:
        """Imgflip's editor with this template loaded, for remaking one by hand."""
        return f"https://imgflip.com/memegenerator/{self.id}"


FORMATS: tuple[Format, ...] = (
    Format(
        "181913649", "Drake Hotline Bling", "https://i.imgflip.com/30b1gx.jpg", 1200, 1200,
        "He waves away the first thing and happily points at the second. The joke is which one "
        "he picks: the lazier, pettier or more chaotic option.",
        (Box(52, 3, 46, 44, "what he refuses: the sensible option", DARK, 45),
         Box(52, 53, 46, 44, "what he picks instead", DARK, 45)),
        ("Fixing the flaky test", "Adding a retry and moving on"),
    ),
    Format(
        "112126428", "Distracted Boyfriend", "https://i.imgflip.com/1ur9b0.jpg", 1200, 800,
        "He turns to stare at someone new while his girlfriend glares. Box 1 labels the tempting "
        "new thing (the woman in red), box 2 labels him, box 3 what he should be loyal to "
        "(the girlfriend).",
        (Box(6, 64, 40, 22, "the tempting new thing", OUTLINE, 30),
         Box(45, 42, 30, 20, "who is tempted, often 'Me'", OUTLINE, 22),
         Box(74, 48, 25, 24, "what is being neglected", OUTLINE, 34)),
        ("A new JS framework", "Me", "The project I started last week"),
    ),
    Format(
        "188390779", "Woman Yelling At Cat", "https://i.imgflip.com/345v97.jpg", 680, 438,
        "She yells an angry accusation; the cat at the dinner table answers, smug and "
        "unbothered. Box 1 is her yell, box 2 the cat's comeback, which is the punchline.",
        (Box(1, 1, 48, 20, "her furious accusation", DARK, 50),
         Box(51, 1, 48, 20, "the cat's smug comeback", DARK, 50)),
        ("It crashes when I click it twice!", "Then don't click it twice"),
    ),
    Format(
        "178591752", "Tuxedo Winnie The Pooh", "https://i.imgflip.com/2ybua0.png", 800, 582,
        "Plain Pooh, then Pooh in a tuxedo: the same thing said plainly, then said grandly. "
        "The joke is the inflated, posh version.",
        (Box(45, 3, 53, 44, "the plain way to say it", DARK, 45),
         Box(45, 53, 53, 44, "the posh way to say exactly the same thing", DARK, 50)),
        ("Copying code from Stack Overflow", "Leveraging community-validated solutions"),
    ),
    Format(
        "252600902", "Always Has Been", "https://i.imgflip.com/46e43q.png", 960, 540,
        "One astronaut gazes at the Earth and realises something; the other, gun raised behind "
        "him, answers. Box 1 is the shocked realisation, 'Wait, it's all ...?'. Box 2 is "
        "'Always has been'.",
        (Box(2, 2, 60, 22, "the realisation: 'Wait, it's all ...?'", OUTLINE, 45),
         Box(63, 2, 35, 16, "'Always has been', or a close variant", OUTLINE, 22)),
        ("Wait, the whole AI is just if statements?", "Always has been"),
    ),
    Format(
        "124822590", "Left Exit 12 Off Ramp", "https://i.imgflip.com/22bdq6.jpg", 804, 767,
        "A car swerves off the highway at the last second. Box 1 (left half of the sign) is the "
        "sensible road straight on, box 2 (right half, the exit) the reckless choice, box 3 the "
        "driver who takes it.",
        (Box(23, 13, 19, 26, "the sensible way, straight on", OUTLINE, 24),
         Box(52, 13, 22, 26, "the reckless exit", OUTLINE, 24),
         Box(28, 60, 44, 24, "who swerves, often 'Me'", OUTLINE, 22)),
        ("Write the tests", "Ship it Friday at 5pm", "Me"),
    ),
    Format(
        "131940431", "Gru's Plan", "https://i.imgflip.com/26jxvz.jpg", 700, 449,
        "Gru presents a plan, one step per board. Steps 1 and 2 sound great, step 3 is the "
        "flaw, and in step 4 he reads step 3 again and realises. Box 4 repeats box 3 word for word.",
        (Box(30.5, 12, 16, 32, "step 1: the plan", DARK, 36, 40),
         Box(80.5, 12, 16, 32, "step 2: it goes well", DARK, 36, 40),
         Box(30.5, 58, 16, 34, "step 3: the catch", DARK, 36, 40),
         Box(81, 58, 15.5, 34, "step 4: the same words as step 3", DARK, 36, 40)),
        ("Ask AI to fix the bug", "It rewrites the whole file", "Now there are four bugs",
         "Now there are four bugs"),
    ),
    Format(
        "93895088", "Expanding Brain", "https://i.imgflip.com/1jwhww.jpg", 857, 1202,
        "Four ideas, each 'smarter' than the last as the brain glows brighter. The joke is that "
        "the last, most enlightened one is the most absurd.",
        (Box(2, 2, 46, 21, "the normal way", DARK, 40),
         Box(2, 27, 46, 21, "a cleverer way", DARK, 40),
         Box(2, 52, 46, 21, "a galaxy-brain way", DARK, 40),
         Box(2, 77, 46, 21, "the most absurd way, presented as enlightenment", DARK, 40)),
        ("Reading the docs", "Asking a coworker", "Asking ChatGPT",
         "Changing random things until it works"),
    ),
    Format(
        "135256802", "Epic Handshake", "https://i.imgflip.com/28j0te.jpg", 900, 645,
        "Two very different groups clasp hands over the one thing they agree on. Box 1 labels "
        "the left arm, box 2 the right arm, box 3 the grip: what they share.",
        (Box(2, 55, 30, 22, "one group (the left arm)", OUTLINE, 26),
         Box(64, 55, 34, 22, "a very different group (the right arm)", OUTLINE, 26),
         Box(24, 6, 36, 16, "what they agree on (the grip)", OUTLINE, 30)),
        ("Frontend devs", "Backend devs", "Blaming the database"),
    ),
    Format(
        "100777631", "Is This A Pigeon", "https://i.imgflip.com/1o00in.jpg", 1587, 1425,
        "A man confidently misidentifies a butterfly. Box 1 labels the man, box 2 the "
        "butterfly, box 3 is his wrong question, 'Is this ...?'.",
        (Box(2, 67, 30, 13, "who is confused", OUTLINE, 24),
         Box(60, 33, 39, 16, "the thing (the butterfly)", OUTLINE, 26),
         Box(3, 82, 94, 16, "his wrong guess: 'Is this ...?'", OUTLINE, 36)),
        ("Product manager", "An if statement", "Is this AI?"),
    ),
    Format(
        "87743020", "Two Buttons", "https://i.imgflip.com/1g8my4.jpg", 600, 908,
        "A man sweats over two buttons he cannot choose between. Box 1 and box 2 are the two "
        "options, box 3 labels the sweating man.",
        (Box(9, 12, 26, 7, "option one (the left button)", DARK, 24),
         Box(44, 7, 26, 7, "option two (the right button)", DARK, 24),
         Box(5, 84, 90, 12, "who has to choose", OUTLINE, 28)),
        ("Read the error message", "Restart and hope", "Me at 2am"),
    ),
    Format(
        "148909805", "Monkey Puppet", "https://i.imgflip.com/2gnnjh.jpg", 923, 768,
        "The puppet glances away, pretending it did not see. One caption: the situation you "
        "would rather not acknowledge.",
        (Box(2, 2, 96, 33, "the awkward situation", DARK, 70),),
        ("When git blame on the bug shows your own name",),
    ),
    Format(
        "99683372", "Sleeping Shaq", "https://i.imgflip.com/1nck6k.jpg", 640, 631,
        "Shaq sleeps through the first thing ('i sleep') and jolts awake for the second "
        "('real shit?'). Box 1 is what he cannot be bothered about, box 2 what suddenly gets his "
        "full attention. The pettier the second, the better.",
        (Box(2, 3, 46, 45, "what he sleeps through", DARK, 45),
         Box(2, 54, 46, 43, "what wakes him right up", DARK, 45)),
        ("Production is down", "Someone used tabs instead of spaces"),
    ),
    Format(
        "226297822", "Panik Kalm Panik", "https://i.imgflip.com/3qqcim.png", 640, 881,
        "Panic, then calm, then worse panic. Box 1 is the scare, box 2 the comforting thought, "
        "box 3 the detail that makes it worse.",
        (Box(2, 2, 44, 30, "the scare", DARK, 40),
         Box(2, 36, 44, 29, "the comforting thought", DARK, 40),
         Box(2, 70, 44, 28, "why it is actually worse", DARK, 40)),
        ("Production is down", "I can roll back", "The rollback script is in production"),
    ),
    Format(
        "247375501", "Buff Doge vs. Cheems", "https://i.imgflip.com/43a45p.png", 937, 720,
        "The mighty buff doge of the past against the feeble cheems of today. Box 1 names the "
        "strong one, box 2 is what they did; box 3 names the weak one, box 4 what they do now.",
        (Box(2, 1, 46, 11, "who the strong one is", DARK, 26),
         Box(2, 75, 46, 23, "what the strong one did", DARK, 40),
         Box(52, 6, 46, 15, "who the weak one is", DARK, 26),
         Box(52, 77, 46, 21, "what the weak one does", DARK, 40)),
        ("Devs in 1995", "Fit a whole game in 64 KB", "Devs in 2026",
         "A to-do app needs 2 GB of node_modules"),
    ),
    Format(
        "89370399", "Roll Safe Think About It", "https://i.imgflip.com/1h7in3.jpg", 702, 395,
        "He taps his temple at his own terrible clever idea. Box 1 is the problem, box 2 his "
        "loophole logic.",
        (Box(2, 2, 96, 26, "the problem you supposedly can't have", OUTLINE, 45),
         Box(2, 69, 96, 22, "if you just ...: the terrible loophole", OUTLINE, 45)),
        ("Can't have merge conflicts", "If you're the only one who commits"),
    ),
    Format(
        "222403160", "Bernie I Am Once Again Asking For Your Support",
        "https://i.imgflip.com/3oevdk.jpg", 750, 750,
        "'I am once again asking' is printed on the image. The text finishes his sentence with "
        "the thing he keeps having to ask for.",
        (Box(5, 87, 90, 12, "how 'I am once again asking...' ends", OUTLINE, 40),),
        ("for you to read the README",),
    ),
    Format(
        "195515965", "Clown Applying Makeup", "https://i.imgflip.com/38el31.jpg", 750, 798,
        "Four steps of putting on clown makeup: each decision a little worse, until the last "
        "one makes you the clown.",
        (Box(2, 2, 52, 20, "the first small decision", DARK, 40),
         Box(2, 27, 52, 20, "a worse one", DARK, 40),
         Box(2, 52, 52, 20, "a worse one still", DARK, 40),
         Box(2, 77, 52, 20, "the last step: full clown", DARK, 40)),
        ("It's a small change", "I'll skip the tests", "I'll deploy on Friday",
         "Who needs a rollback"),
    ),
    Format(
        "180190441", "They're The Same Picture", "https://i.imgflip.com/2za3u1.jpg", 1363, 1524,
        "Pam is asked to find the differences between two pictures and says they are the same "
        "picture. Box 1 and box 2 are two things that sound different but are the same; "
        "'They're the same picture' is printed on the image.",
        (Box(10, 8, 30, 20, "one thing", DARK, 30),
         Box(55, 15, 33, 22, "another thing that is secretly the same", DARK, 30)),
        ("It works on my machine", "I didn't test it"),
    ),
    Format(
        "110163934", "I Bet He's Thinking About Other Women", "https://i.imgflip.com/1tl71a.jpg",
        1654, 930,
        "In bed, she suspects he is thinking about other women; he is lost in some nerdy "
        "problem. Box 1 is her suspicion (keep 'I bet he's thinking about other women' or a "
        "close variant), box 2 what he is really thinking.",
        (Box(2, 52, 40, 22, "her suspicion", OUTLINE, 45),
         Box(46, 72, 52, 25, "what he is really thinking", OUTLINE, 55)),
        ("I bet he's thinking about other women", "Why does it pass locally but fail in CI?"),
    ),
    Format(
        "322841258", "Anakin Padme 4 Panel", "https://i.imgflip.com/5c7lwq.png", 768, 768,
        "Anakin states his plan; Padme smiles and asks for the obvious reassurance; Anakin just "
        "stares; Padme asks again, worried. Box 1 is his plan, box 2 her hopeful question, box 3 "
        "the same question again.",
        (Box(2, 36, 46, 13, "his plan", OUTLINE, 36),
         Box(52, 36, 46, 13, "her hopeful question", OUTLINE, 30),
         Box(52, 86, 46, 13, "the same question again, now worried", OUTLINE, 30)),
        ("I'm rewriting the auth system", "With tests, right?", "With tests, right?"),
    ),
    Format(
        "247113703", "A train hitting a school bus", "https://i.imgflip.com/434i5j.png", 920, 1086,
        "A train obliterates a school bus. Box 1 labels the train, the unstoppable thing; box 2 "
        "labels the bus, what gets wrecked.",
        (Box(8, 52, 40, 14, "the unstoppable thing (the train)", OUTLINE, 30),
         Box(52, 74, 46, 16, "what gets wrecked (the bus)", OUTLINE, 30)),
        ("One npm update", "My working project"),
    ),
    Format(
        "354700819", "Two guys on a bus", "https://i.imgflip.com/5v6gwj.jpg", 762, 675,
        "On a bus, one man faces a rock wall, miserable; the other faces a beautiful view, "
        "delighted. Box 1 labels the miserable side, box 2 the happy side.",
        (Box(2, 20, 34, 24, "the miserable side (facing the rock)", OUTLINE, 30),
         Box(45, 6, 53, 20, "the happy side (facing the view)", OUTLINE, 40)),
        ("Me fixing prod on Friday night", "The PM who said 'just a small change'"),
    ),
)

BY_ID = {f.id: f for f in FORMATS}
