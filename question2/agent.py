
from __future__ import annotations

import json
from dataclasses import dataclass

from shared.billing import CostSession
from shared.llm import ChatMessage, LlmClient
from shared.tools import format_thread, get_order, get_ticket

from question1.agent import parse_resolution


@dataclass
class TicketResolution:
    action: str
    kb_article: str


class SupportPipeline:
    """Four-stage ShopAssist pipeline with cost-efficient processing."""

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
        final = self._audit(snapshot, draft)

        parsed = parse_resolution(final)

        return TicketResolution(
            parsed.action,
            parsed.kb_article
        )

    def _triage(self, ticket_id: str, thread: str, order: dict) -> dict:
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
    def _retrieve(self, triage: dict) -> dict:
        candidates = []
        product_type = triage["product_type"]

        if product_type == "subscription":
            candidates.append("subscription-cooling-off")

        elif triage["license_revealed"]:
            candidates.append("digital-license-no-refund")

        elif triage["customized"] and triage["damage_reported"]:
            candidates.append("custom-items-no-replace")

        elif (
            triage["damage_reported"]
            and not triage["customized"]
            and triage["delivered_days_ago"] <= 7
        ):
            candidates.append("damage-replacement-7-days")

        elif (
            product_type == "physical"
            and not triage["opened"]
            and triage["delivered_days_ago"] <= 30
        ):
            candidates.append("returns-within-30-days")

        else:
            candidates.append("returns-within-30-days")

        return {"candidate_articles": candidates}

    def _compose(
        self,
        ticket_id: str,
        snapshot: str,
        triage: dict,
        retrieved: dict,
    ) -> dict:
        return {
            "ticket_id": ticket_id,
            "snapshot": snapshot,
            "triage": triage,
            "retrieved": retrieved,
        }

    def _audit(self, snapshot: str, draft: dict) -> str:
        system = (
            "You are ShopAssist. "
            "Return concise JSON with action, kb_article, and customer_reply. "
            "Use the order snapshot and retrieved candidate articles."
        )

        pipeline_context = {
            "ticket_id": draft["ticket_id"],
            "product_type": draft["triage"]["product_type"],
            "damage_reported": draft["triage"]["damage_reported"],
            "customized": draft["triage"]["customized"],
            "license_revealed": draft["triage"]["license_revealed"],
            "subscription_age_days": draft["triage"]["subscription_age_days"],
            "candidate_articles": draft["retrieved"]["candidate_articles"],
        }

        user = (
            f"ORDER_SNAPSHOT:\n{snapshot}\nEND_SNAPSHOT\n\n"
            f"PIPELINE_CONTEXT:\n"
            f"{json.dumps(pipeline_context, separators=(',', ':'))}"
        )

        messages = [
            ChatMessage("system", system),
            ChatMessage("user", user),
        ]

        return self.llm.chat("audit", messages)
