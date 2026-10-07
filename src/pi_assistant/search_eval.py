"""A small memory-search test for comparing embeddings models on your own hardware.

It embeds a fixed set of made-up memories (facts, notes and past conversations) the way
Athena does, then checks that questions find the right one, that unrelated questions
don't pull anything in, and where the auto-recall and duplicate cut-offs should sit.

    pi-assistant embeddings test --model embeddinggemma --model embeddinggemma-2:740m-bf16
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from openai import APIConnectionError

from pi_assistant.config import EmbeddingsConfig, MemoryConfig
from pi_assistant.memory import Embedder

# (kind, text, source). Made up: none of this is about anyone real.
MEMORIES: list[tuple[str, str, str | None]] = [
    (
        "fact",
        "The boiler was last serviced on 12 March 2026 by Hartley Heating; the next one is due in March 2027.",
        None,
    ),
    ("fact", "My passport expires on 4 August 2029.", None),
    ("fact", "Sam is allergic to peanuts and shellfish.", None),
    ("fact", "The Wi-Fi network at the cottage is called Bramble.", None),
    ("fact", "Priya's birthday is 14 November.", None),
    ("fact", "Alex's birthday is 3 May.", None),
    ("fact", "I prefer aisle seats on flights.", None),
    ("fact", "The car is a 2019 Skoda Octavia estate and its MOT is due on 21 January.", None),
    ("fact", "The bins go out on Tuesday night: recycling one week, general waste the next.", None),
    ("fact", "Dr Okafor is my GP at the Riverside surgery.", None),
    ("fact", "I take 10 mg of cetirizine during hay fever season.", None),
    ("fact", "My running goal this year is a 10K in under 50 minutes.", None),
    ("fact", "The home insurance renews on 1 September with Lumen Insurance.", None),
    ("fact", "I have Spanish lessons on Tuesdays and Thursdays.", None),
    ("fact", "The spare house key is with the neighbours at number 18.", None),
    ("fact", "I don't eat red meat.", None),
    ("fact", "The company's financial year ends on 31 March.", None),
    ("fact", "My locker at the gym is number 42.", None),
    (
        "document",
        "Feed the starter twice a day at room temperature and bake when it doubles within 4 to 6 hours. "
        "Dough: 500 g strong white flour, 350 g water, 100 g starter, 10 g salt.",
        "notes/sourdough.md",
    ),
    (
        "document",
        "Tomatoes go in the greenhouse in mid-April and outside after the last frost, usually late May. "
        "Water them in the evening.",
        "notes/garden.md",
    ),
    ("document", "Tyre pressures: 80 psi front, 85 psi rear. Lube the chain every 300 km.", "notes/bike.md"),
    (
        "document",
        "Day one: Fushimi Inari at dawn. Day two: the Arashiyama bamboo grove, then lunch at Nishiki market.",
        "notes/kyoto-trip.md",
    ),
    (
        "document",
        "The NAS backs up every night at 02:00 with restic. Check it with `restic snapshots`; prune monthly.",
        "notes/backups.md",
    ),
    (
        "conversation",
        "User: What's a good stretch for lower back pain?\nAssistant: Try a knee-to-chest stretch: lie on your "
        "back and hug one knee for 30 seconds, then switch.",
        None,
    ),
    (
        "conversation",
        "User: How long should I boil an egg for a runny yolk?\nAssistant: About six and a half minutes from "
        "boiling, then into cold water.",
        None,
    ),
    ("conversation", "User: Can you convert 30 miles to kilometres?\nAssistant: 30 miles is about 48.3 km.", None),
    ("conversation", "User: Remind me what the capital of Australia is.\nAssistant: Canberra.", None),
    (
        "conversation",
        "User: Which film should we watch on Friday, Past Lives or Aftersun?\nAssistant: I'd go for Aftersun.",
        None,
    ),
    # Look-alikes, which the questions shouldn't pick
    ("fact", "Sam's dentist appointment is on 9 December.", None),
    ("fact", "The car insurance renews on 2 February with Lumen Insurance.", None),
    ("fact", "Alex is vegetarian.", None),
    ("fact", "The flat in Leeds has the Wi-Fi network Heron.", None),
    (
        "document",
        "Pizza dough: 500 g 00 flour, 325 g water, 7 g dried yeast, 10 g salt. Prove for 2 hours.",
        "notes/pizza.md",
    ),
]

# (question, the memory it should find)
QUESTIONS: list[tuple[str, int]] = [
    ("When is the boiler due to be serviced?", 0),
    ("When do I need to renew my passport?", 1),
    ("¿Cuándo caduca mi pasaporte?", 1),
    ("What can't Sam eat?", 2),
    ("What's the wifi called at the cottage?", 3),
    ("When is Priya's birthday?", 4),
    ("Quand est l'anniversaire de Priya ?", 4),
    ("Whose birthday is in May?", 5),
    ("Window or aisle seat?", 6),
    ("When's the car's MOT?", 7),
    ("Which bin goes out this week?", 8),
    ("Who's my doctor?", 9),
    ("What antihistamine do I take?", 10),
    ("What's my 10K target?", 11),
    ("Who are we insured with for the house?", 12),
    ("When are my language classes?", 13),
    ("I'm locked out, where's the spare key?", 14),
    ("Do I have any dietary restrictions?", 15),
    ("When does the fiscal year end at work?", 16),
    ("Which locker is mine at the gym?", 17),
    ("How much water goes in the sourdough?", 18),
    ("When can I plant tomatoes outside?", 19),
    ("What pressure should my bike tyres be?", 20),
    ("What are we doing on the first day in Kyoto?", 21),
    ("How do I check the backups worked?", 22),
    ("What did you suggest for my back?", 23),
    ("Soft-boiled egg timing", 24),
    ("How many km is 30 miles?", 25),
    ("Australia's capital", 26),
    ("Which film did you recommend for Friday?", 27),
    # Indirect: the question doesn't use the memory's words
    ("I need to book the heating engineer, when's it due?", 0),
    ("Is it OK to make a satay for Sam?", 2),
    ("What present should I get for the November birthday?", 4),
    ("Is the car's roadworthiness test coming up?", 7),
    ("What should I put out on Tuesday?", 8),
    ("My hay fever is starting, what do I usually take?", 10),
    ("Is anything due on the house insurance soon?", 12),
    ("What should I do with the starter before baking bread?", 18),
    ("When should I oil the bike chain?", 20),
    ("How did we decide what to watch?", 27),
]

# Questions none of the memories answer: nothing should be recalled for them.
UNRELATED = [
    "What's the weather in Lisbon tomorrow?",
    "Write me a haiku about autumn.",
    "What's 17 times 23?",
    "Who won the 1966 World Cup?",
    "Translate 'good morning' into Japanese.",
    "Summarise today's news headlines.",
    "Set a timer for ten minutes.",
    "What's the population of Canada?",
    "Tell me a joke.",
    "How do black holes form?",
]

# Saving the second of each pair should find the first and not save it again...
DUPLICATES = [
    ("My passport expires on 4 August 2029.", "My passport expires on 4 August 2029"),
    ("Sam is allergic to peanuts and shellfish.", "Sam is allergic to shellfish and peanuts."),
    ("I prefer aisle seats on flights.", "I prefer aisle seats when flying."),
    ("The bins go out on Tuesday night.", "the bins go out on tuesday night"),
]
# ...but these are different facts, so both should be kept.
DIFFERENT = [
    ("Priya's birthday is 14 November.", "Alex's birthday is 3 May."),
    ("My locker at the gym is number 42.", "My locker at the gym is number 24."),
    ("The car's MOT is due on 21 January.", "The car's insurance is due on 21 January."),
    ("I don't eat red meat.", "I don't eat fish."),
    ("Dr Okafor is my GP.", "Dr Okafor is Sam's GP."),
]


@dataclass
class SearchReport:
    model: str
    error: str = ""
    first_seconds: float = 0.0  # the first request, which includes loading the model if it isn't yet
    per_text_ms: float = 0.0
    top1: int = 0
    top3: int = 0
    misses: list[str] = field(default_factory=list)  # "question → what it found instead"
    right: list[float] = field(default_factory=list)  # distance to the right memory, per question
    unrelated: list[float] = field(default_factory=list)  # distance to the nearest memory, per unrelated question
    duplicates: list[float] = field(default_factory=list)
    different: list[float] = field(default_factory=list)

    @property
    def questions(self) -> int:
        return len(self.right)

    def recall(self, cutoff: float) -> tuple[int, int]:
        """(right memories kept, unrelated questions that would still pull one in) at ``cutoff``."""
        return sum(d <= cutoff for d in self.right), sum(d <= cutoff for d in self.unrelated)

    def best_recall_cutoff(self) -> float:
        """The recall_max_distance that keeps the most right memories while letting in the fewest unrelated ones."""
        steps = [round(0.05 + i / 100, 2) for i in range(86)]  # 0.05 .. 0.90
        n, m = max(len(self.right), 1), max(len(self.unrelated), 1)
        scores = [self.recall(c)[0] / n - self.recall(c)[1] / m for c in steps]
        tied = [c for c, s in zip(steps, scores, strict=True) if s == max(scores)]
        return tied[len(tied) // 2]

    def duplicates_at(self, cutoff: float) -> tuple[int, int]:
        """(repeated facts caught, different facts wrongly merged) at ``cutoff``."""
        return sum(d < cutoff for d in self.duplicates), sum(d < cutoff for d in self.different)

    def best_duplicate_cutoff(self) -> float | None:
        """A duplicate_distance a third of the way from the furthest repeat to the closest different fact,
        erring towards keeping both. None if the two overlap, so no cut-off separates them."""
        low, high = max(self.duplicates), min(self.different)
        if low >= high:
            return None
        return round(low + (high - low) / 3, 3)


def _distance(a: list[float], b: list[float]) -> float:
    return 1.0 - sum(x * y for x, y in zip(a, b, strict=True))  # both are unit length


def _title(kind: str, source: str | None) -> str:
    if kind == "document" and source:
        return source.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return "conversation" if kind == "conversation" else "none"


async def run_search_test(cfg: EmbeddingsConfig, model: str | None = None) -> SearchReport:
    cfg = cfg.model_copy(update={"model": model or cfg.model})
    report = SearchReport(cfg.model)
    embedder = Embedder(cfg)
    try:
        started = time.perf_counter()
        await embedder.embed(["warm-up"], "query")
        report.first_seconds = time.perf_counter() - started

        started = time.perf_counter()
        docs = await embedder.embed([t for _, t, _ in MEMORIES], "document", [_title(k, s) for k, _, s in MEMORIES])
        report.per_text_ms = (time.perf_counter() - started) * 1000 / len(MEMORIES)
        questions = await embedder.embed([q for q, _ in QUESTIONS] + UNRELATED, "query")
        pairs = await embedder.embed([t for pair in DUPLICATES + DIFFERENT for t in pair], "document")
    except Exception as exc:
        report.error = describe_embeddings_error(exc, cfg.model)
        return report
    finally:
        await embedder.close()

    for (question, want), vector in zip(QUESTIONS, questions, strict=False):
        order = sorted(range(len(docs)), key=lambda i: _distance(vector, docs[i]))
        rank = order.index(want) + 1
        report.top1 += rank == 1
        report.top3 += rank <= 3
        report.right.append(_distance(vector, docs[want]))
        if rank > 1:
            report.misses.append(f"{question} → {MEMORIES[order[0]][1][:60]}")
    for vector in questions[len(QUESTIONS) :]:
        report.unrelated.append(min(_distance(vector, d) for d in docs))
    for i in range(0, len(pairs), 2):
        target = report.duplicates if i // 2 < len(DUPLICATES) else report.different
        target.append(_distance(pairs[i], pairs[i + 1]))
    return report


def describe_embeddings_error(exc: Exception, model: str) -> str:
    text = str(exc)
    if "not found" in text.lower():
        return f"the embeddings server doesn't have {model}. For Ollama: ollama pull {model}"
    if isinstance(exc, APIConnectionError):
        return "couldn't reach the embeddings server"
    return f"{type(exc).__name__}: {text}"


def print_report(r: SearchReport, memory: MemoryConfig) -> None:
    print(f"\n{r.model}")
    if r.error:
        print(f"  error: {r.error}")
        return
    n, unrelated = r.questions, len(r.unrelated)
    kept, leaked = r.recall(memory.recall_max_distance)
    best = r.best_recall_cutoff()
    best_kept, best_leaked = r.recall(best)
    caught, merged = r.duplicates_at(memory.duplicate_distance)
    dup = r.best_duplicate_cutoff()
    print(f"  speed:        {r.per_text_ms:.0f} ms a memory (the first request took {r.first_seconds:.1f}s)")
    print(f"  search:       finds the right memory first for {r.top1}/{n} questions, in the top 3 for {r.top3}/{n}")
    print(
        f"  auto-recall:  at your cut-off of {memory.recall_max_distance:.2f}, keeps {kept}/{n} right memories "
        f"and brings one in for {leaked}/{unrelated} unrelated questions"
    )
    print(
        f"                at {best:.2f}, the best for this model, keeps {best_kept}/{n} "
        f"and brings one in for {best_leaked}/{unrelated}"
    )
    print(
        f"  duplicates:   at your cut-off of {memory.duplicate_distance}, catches {caught}/{len(r.duplicates)} "
        f"repeated facts and merges {merged}/{len(r.different)} different ones"
        + (f"; {dup} would suit this model" if dup is not None else "; no cut-off separates them")
    )
    print(f"  distances:    right memories {_spread(r.right)}, unrelated questions {_spread(r.unrelated)}")
    for miss in r.misses:
        print(f"  missed:       {miss}")


def _spread(values: list[float]) -> str:
    return f"{min(values):.2f}–{max(values):.2f} (median {statistics.median(values):.2f})" if values else "-"
