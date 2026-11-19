
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from shared.billing import CostSession
from shared.kb import article_ids, load_article
from shared.llm import ChatMessage, LlmClient
from shared.tools import format_thread, get_order, get_ticket

from question1.agent import parse_resolution


@dataclass
class TicketResolution:
    action: str
    kb_article: str


class SupportPipeline:
    """Cost-efficient four-stage ShopAssist support pipeline."""

    VALID_ACTIONS = {
        "REFUND",
        "REPLACE",
        "SEND_ARTICLE",
        "ESCALATE_TIER2",
    }

    STOPWORDS = {
        "the", "a", "an", "is", "are", "was", "were",
        "i", "we", "you", "my", "our", "your", "it",
        "to", "for", "of", "on", "in", "at", "and",
        "or", "but", "can", "could", "please", "me",
        "they", "this", "that", "with", "have", "has",
        "had", "be", "been", "do", "did", "does",
        "get", "got", "just", "already", "now", "day",
    }

    def __init__(self, session: CostSession | None = None):
        self.session = session or CostSession()
        self.llm = LlmClient(self.session)

    def handle_ticket(self, ticket_id: str) -> TicketResolution:
        ticket = get_ticket(ticket_id)
        order = get_order(ticket["order_id"])
        thread = format_thread(ticket)
        snapshot = json.dumps(order, separators=(",", ":"))

        # Stage 1: Triage
        triage = self._triage(ticket_id, thread, order)

        # Stage 2: Retrieve
        retrieved = self._retrieve(triage)

        # Stage 3: Compose
        draft = self._compose(
            ticket_id, snapshot, triage, retrieved
        )

        # Stage 4: Audit
        allowed_articles = {
            item["article_id"] for item in retrieved["articles"]
        }
        return self._audit(snapshot, draft, allowed_articles)

    def _triage(
        self, ticket_id: str, thread: str, order: dict
    ) -> dict:
        return {
            "ticket_id": ticket_id,
            "thread": thread,
            "product_type": order.get("product_type"),
            "delivered_days_ago": order.get("delivered_days_ago", 0),
            "opened": order.get("opened", False),
            "license_revealed": order.get("license_revealed", False),
            "customized": order.get("customized", False),
            "damage_reported": order.get("damage_reported", False),
            "subscription_age_days": order.get("subscription_age_days", 0),
        }

    @classmethod
    def _terms(cls, text: str) -> set[str]:
        words = re.findall(r"[a-z0-9]+", text.lower())
        return {
            word for word in words
            if len(word) > 2 and word not in cls.STOPWORDS
        }

    def _retrieve(self, triage: dict) -> dict:
        query = (
            triage["thread"]
            + " "
            + json.dumps(triage, separators=(",", ":"))
        )
        query_terms = self._terms(query)

        ranked = []

        for article_id in article_ids():
            content = load_article(article_id)
            article_terms = self._terms(
                article_id.replace("-", " ") + " " + content
            )

            overlap = query_terms & article_terms

            # Base relevance from actual article content.
            score = len(overlap)

            # Policy-specific signals improve ranking where
            # generic words such as "replacement" are ambiguous.
            if article_id == "subscription-cooling-off":
                if triage["product_type"] == "subscription":
                    score += 8

            elif article_id == "digital-license-no-refund":
                if triage["license_revealed"]:
                    score += 8

            elif article_id == "custom-items-no-replace":
                if triage["customized"] and triage["damage_reported"]:
                    score += 10

            elif article_id == "damage-replacement-7-days":
                if (
                    triage["damage_reported"]
                    and not triage["customized"]
                    and triage["delivered_days_ago"] <= 7
                ):
                    score += 10

            elif article_id == "returns-within-30-days":
                if (
                    triage["product_type"] == "physical"
                    and not triage["opened"]
                    and triage["delivered_days_ago"] <= 30
                ):
                    score += 8

            ranked.append({
                "article_id": article_id,
                "score": score,
                "content": content,
            })

        ranked.sort(
            key=lambda item: (-item["score"], item["article_id"])
        )

        return {"articles": ranked[:3]}

    def _compose(
        self,
        ticket_id: str,
        snapshot: str,
        triage: dict,
        retrieved: dict,
    ) -> str:
        articles = [
            {
                "article_id": item["article_id"],
                "content": item["content"],
            }
            for item in retrieved["articles"]
        ]

        system = (
            "You are ShopAssist, a customer support assistant. "
            "Use the customer conversation, order snapshot, and "
            "retrieved help-center policies to determine the correct "
            "resolution. Follow the most specific applicable policy. "
            "Return only JSON with action, kb_article, and customer_reply. "
            "The action must be REFUND, REPLACE, SEND_ARTICLE, "
            "or ESCALATE_TIER2. Keep the reply concise."
        )

        context = {
            "ticket_id": ticket_id,
            "customer_thread": triage["thread"],
            "triage": {
                key: value
                for key, value in triage.items()
                if key not in {"thread", "ticket_id"}
            },
            "retrieved_articles": articles,
        }

        user = (
            f"ORDER_SNAPSHOT:\n{snapshot}\nEND_SNAPSHOT\n\n"
            f"CASE_CONTEXT:\n"
            f"{json.dumps(context, separators=(',', ':'))}"
        )

        messages = [
            ChatMessage("system", system),
            ChatMessage("user", user),
        ]

        return self.llm.chat("compose", messages)

    def _is_valid(
        self,
        result: TicketResolution,
        allowed_articles: set[str],
    ) -> bool:
        return (
            result.action in self.VALID_ACTIONS
            and result.kb_article in article_ids()
            and result.kb_article in allowed_articles
        )


    def _audit(
        self,
        snapshot: str,
        draft: str,
        allowed_articles: set[str],
    ) -> TicketResolution:

    # Validate the first response locally.
        try:
            parsed = parse_resolution(draft)

            if self._is_valid(parsed, allowed_articles):
                return TicketResolution(
                    parsed.action,
                    parsed.kb_article,
                )

        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass

        # Retry only if the response is malformed or invalid.
        repair_system = (
            "Return a valid JSON support resolution. "
            "Use one of these actions: REFUND, REPLACE, "
            "SEND_ARTICLE, ESCALATE_TIER2. "
            f"The kb_article must be one of: "
            f"{', '.join(sorted(allowed_articles))}. "
            "Return action, kb_article, and customer_reply only."
        )

        repair_user = (
            f"ORDER_SNAPSHOT:\n{snapshot}\nEND_SNAPSHOT\n\n"
            f"PREVIOUS_RESPONSE:\n{draft}"
        )

        repaired = self.llm.chat(
            "audit_retry",
            [
                ChatMessage("system", repair_system),
                ChatMessage("user", repair_user),
            ],
        )

        result = parse_resolution(repaired)

        if not self._is_valid(result, allowed_articles):
            raise ValueError(
                f"Invalid resolution after retry: {result}"
            )

        return TicketResolution(result.action, result.kb_article)
