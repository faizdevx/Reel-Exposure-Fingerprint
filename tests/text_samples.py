"""Synthetic captions for sanity checks of the text pipeline. NOT the M3 label audit.

Hand-written for this repo, not taken from Instagram: no real handle, caption or person. Each
sample names the taxonomy topic a reader would expect. They check that zero-shot scoring is
plausible in English, Hindi and Hinglish, and that emoji and hashtags help. They are too few and
too easy to estimate accuracy; the 200-Reel hand-labelled audit (roadmap E6) does that.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    slug: str
    lang: str
    caption: str | None
    hashtags: tuple[str, ...] | None = None


SAMPLES: tuple[Sample, ...] = (
    Sample(
        "comedy_skits",
        "en",
        "When your teacher says there is a surprise test tomorrow 😂",
        ("comedy", "funnyskit"),
    ),
    Sample("comedy_skits", "hi", "जब टीचर बोले कल टेस्ट है 😂😂", ("मज़ेदार", "कॉमेडी")),
    Sample(
        "memes_trends",
        "en",
        "this trend is taking over the internet, use this audio for your meme",
        ("viral", "trending"),
    ),
    Sample(
        "music_dance",
        "en",
        "Dance cover on the new song, choreography by me 💃",
        ("dance", "reels"),
    ),
    Sample("music_dance", "hi", "इस गाने पर मेरा डांस कवर देखिए 🎶", ("डांस",)),
    Sample(
        "entertainment_celebrity",
        "en",
        "Official trailer of the new bollywood movie, in cinemas this Friday 🎬",
        ("bollywood", "movie"),
    ),
    Sample(
        "cricket",
        "en",
        "Kohli hits a six in the last over, what a match! IPL highlights 🏏",
        ("ipl", "cricket"),
    ),
    Sample("cricket", "hi", "आख़िरी ओवर में छक्का, क्या मैच था! आईपीएल हाइलाइट्स 🏏", ("क्रिकेट",)),
    Sample(
        "sports_other",
        "en",
        "Football goal of the season, unbelievable strike from outside the box ⚽",
        ("football",),
    ),
    Sample(
        "fitness_workout",
        "en",
        "Full back and biceps workout at the gym, 4 sets of 12 💪",
        ("gym", "fitness"),
    ),
    Sample(
        "fitness_workout",
        "hinglish",
        "Ghar pe hi karo ye workout, weight loss ke liye best hai",
        ("homeworkout", "weightloss"),
    ),
    Sample(
        "food_cooking",
        "en",
        "Easy paneer butter masala recipe, ready in 20 minutes 🍛",
        ("recipe", "foodie"),
    ),
    Sample("food_cooking", "hi", "घर पर बनाइए स्वादिष्ट पनीर बटर मसाला, आसान रेसिपी", ("रेसिपी",)),
    Sample(
        "travel",
        "en",
        "Three days in Manali: budget, itinerary and best places to visit ✈️",
        ("travelvlog", "manali"),
    ),
    Sample(
        "fashion_style",
        "en",
        "Five saree draping styles for a wedding look, outfit ideas",
        ("fashion", "ootd"),
    ),
    Sample(
        "beauty_skincare",
        "en",
        "My night skincare routine for glowing skin, products I use daily",
        ("skincare",),
    ),
    Sample(
        "tech_gadgets",
        "en",
        "iPhone 17 vs Galaxy S26 camera comparison and honest review 📱",
        ("techreview", "smartphone"),
    ),
    Sample(
        "ai_programming",
        "en",
        "Build a chatbot with Python in 10 minutes, full coding tutorial",
        ("python", "AIAgents", "coding"),
    ),
    Sample("gaming", "en", "BGMI squad wipe, tournament clutch moments 🎮", ("bgmi", "esports")),
    Sample("education_study", "hi", "यूपीएससी की तैयारी कैसे शुरू करें, पूरी रणनीति", ("upsc", "study")),
    Sample(
        "business_entrepreneurship",
        "en",
        "How I started my first startup with zero investment, lessons for founders",
        ("startup", "entrepreneur"),
    ),
    Sample(
        "personal_finance",
        "en",
        "Start a SIP of 5000 rupees a month, mutual fund investing for beginners 📈",
        ("investing", "mutualfunds"),
    ),
    Sample(
        "motivation_selfimprovement",
        "en",
        "Discipline beats motivation. Wake up at 5am and change your life",
        ("motivation", "mindset"),
    ),
    Sample("spirituality_religion", "hi", "आज का भजन, जय श्री राम 🙏 मंदिर में आरती दर्शन", ("भजन",)),
    Sample(
        "news_politics",
        "en",
        "Breaking: Prime minister addresses the nation, new government policy announced",
        ("news", "politics"),
    ),
    Sample(
        "science_facts",
        "en",
        "Why is the sky blue? The science of light explained in 30 seconds",
        ("science", "didyouknow"),
    ),
    Sample(
        "health_wellness",
        "en",
        "A doctor explains five signs of vitamin D deficiency and what to eat",
        ("health", "doctor"),
    ),
    Sample(
        "relationships_dating",
        "en",
        "Signs he likes you: dating advice and relationship tips for couples ❤️",
        ("dating", "relationship"),
    ),
    Sample(
        "parenting_family",
        "en",
        "Toddler tantrums: parenting tips every new mom should know",
        ("parenting", "momlife"),
    ),
    Sample(
        "pets_animals",
        "en",
        "Puppy learns to sit in one day, dog training tips 🐶",
        ("dogsofinstagram",),
    ),
    Sample(
        "automobiles",
        "en",
        "New Royal Enfield launch, first ride review and mileage test 🏍️",
        ("bikes", "carreview"),
    ),
    Sample(
        "home_diy",
        "en",
        "DIY room decor ideas on a budget, easy home makeover and plant care 🪴",
        ("diy", "homedecor"),
    ),
    Sample(
        "art_photography",
        "en",
        "Watercolor painting process, time lapse of a landscape drawing 🎨",
        ("art", "painting"),
    ),
    # Hashtags only, caption missing: the text still carries the topic.
    Sample("cricket", "en", None, ("IPL2026", "CricketLovers", "ViratKohli")),
    Sample("food_cooking", "hinglish", None, ("streetfood", "foodie", "recipes")),
)
