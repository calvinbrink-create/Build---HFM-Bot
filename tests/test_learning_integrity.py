from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cipherfx_platform.contracts import TradeProposal
from cipherfx_platform.database import DatabaseLayer
from cipherfx_platform.feedback import LearningFeedbackEngine
from cipherfx_platform.engines import LearningEngine


MODEL = "cfx-learning-v2"


def proposal(proposal_id="p1"):
    now = datetime.now(timezone.utc)
    return TradeProposal(
        proposal_id=proposal_id,
        symbol="EURUSD",
        asset_class="forex",
        side="BUY",
        entry_price=1.1000,
        stop_loss=1.0990,
        take_profit=1.1020,
        volume_hint=0.0,
        strategy_name="FOREX",
        score=82.0,
        score_components={"timeframes": {"H4": 80.0}},
        created_at=now,
        expires_at=now + timedelta(seconds=30),
        context={
            "engine": "FOREX",
            "asset_class": "forex",
            "model_version": MODEL,
            "probability_source": "PRIOR_ONLY",
            "probability_score_bucket": 80,
        },
        confidence=50.0,
        probability=50.0,
        reasoning=("test",),
        risk_amount=10.0,
    )


class LearningIntegrityTests(unittest.TestCase):
    def test_closed_trade_is_traceable_and_learning_eligible(self):
        with TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            item = proposal()
            db.save_proposal(item)
            feedback = LearningFeedbackEngine(db, LearningEngine(db).record_outcome)
            feedback.record_closed_trade(
                item.proposal_id,
                item.symbol,
                item.side,
                20.0,
                10.0,
                "TP",
                asset_class="forex",
                proposal_id=item.proposal_id,
            )
            rows = db.engine_history(
                "FOREX", model_version=MODEL, learning_only=True
            )
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["metrics"]["learning_eligible"])
            self.assertEqual(rows[0]["metrics"]["proposal_score"], 82.0)
            self.assertEqual(rows[0]["metrics"]["outcome_type"], "closed_trade")

    def test_existing_sparse_trade_is_enriched_without_rewriting_pnl(self):
        with TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            item = proposal("sparse-existing")
            db.save_proposal(item)
            closed_at = datetime.now(timezone.utc)
            db.record_trade_history(
                item.proposal_id, item.proposal_id, item.symbol, item.asset_class,
                item.side, -12.5, 10.0, -1.25, "BROKER_HISTORY", closed_at,
                {"profit": -12.5, "management_rank": "DANGER"},
            )
            feedback = LearningFeedbackEngine(db, LearningEngine(db).record_outcome)
            feedback.record_closed_trade(
                item.proposal_id, item.symbol, item.side, -12.5, 10.0,
                "BROKER_HISTORY", closed_at=closed_at, asset_class=item.asset_class,
                proposal_id=item.proposal_id, metrics={"profit": -12.5},
            )
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT realized_pnl,metrics_json FROM trade_history WHERE trade_id=?",
                    (item.proposal_id,),
                ).fetchone()
            saved = __import__("json").loads(row[1])
            self.assertEqual(row[0], -12.5)
            self.assertEqual(saved["proposal_score"], 82.0)
            self.assertEqual(saved["proposal_confidence"], 50.0)
            self.assertEqual(saved["proposal_probability"], 50.0)
            self.assertFalse(saved["proposal_trace_missing"])
            self.assertEqual(len(db.engine_history("FOREX")), 1)

    def test_expired_proposal_never_enters_learning_history(self):
        with TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            item = proposal("expired")
            db.save_proposal(item)
            feedback = LearningFeedbackEngine(db, LearningEngine(db).record_outcome)
            feedback.record_expired_proposal(
                item.proposal_id, item.symbol, item.side, item.asset_class
            )
            self.assertEqual(len(db.engine_history("FOREX")), 1)
            self.assertEqual(
                db.engine_history(
                    "FOREX", model_version=MODEL, learning_only=True
                ),
                [],
            )

    def test_probability_is_prior_only_before_thirty_valid_outcomes(self):
        with TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            value = db.calibrated_probability("FOREX", "BUY", 82.0, MODEL)
            self.assertEqual(value["status"], "PRIOR_ONLY")
            self.assertEqual(value["sample_size"], 0)
            self.assertEqual(value["probability"], 50.0)

    def test_probability_calibrates_only_current_model_valid_outcomes(self):
        with TemporaryDirectory() as tmp:
            db = DatabaseLayer(Path(tmp) / "platform.db")
            for index in range(30):
                result = 1.0 if index < 20 else -1.0
                db.record_engine_outcome(
                    "FOREX",
                    f"t{index}",
                    "EURUSD",
                    result,
                    result * 10.0,
                    metrics={
                        "model_version": MODEL,
                        "outcome_type": "closed_trade",
                        "learning_eligible": True,
                        "result_r_valid": True,
                        "side": "BUY",
                        "score_bucket": 80,
                    },
                )
            db.record_engine_outcome(
                "FOREX",
                "expired-old",
                "EURUSD",
                0.0,
                0.0,
                metrics={
                    "model_version": "old-model",
                    "outcome_type": "expired_proposal",
                    "learning_eligible": False,
                    "result_r_valid": False,
                    "side": "BUY",
                    "score_bucket": 80,
                },
            )
            value = db.calibrated_probability("FOREX", "BUY", 82.0, MODEL)
            self.assertEqual(value["status"], "CALIBRATED")
            self.assertEqual(value["sample_size"], 30)
            self.assertEqual(value["wins"], 20)
            self.assertEqual(value["losses"], 10)
            self.assertGreater(value["probability"], 50.0)


if __name__ == "__main__":
    unittest.main()
