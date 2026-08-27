# tests/scenarios/test_simulation_scenarios.py
import unittest
from datetime import datetime, timedelta, timezone
from src.sim.fake_store import FakeStore
from src.sim.persona import ScriptedPersona
from src.sim.scenario_runner import run_simulation
from src.types.entities import Commitment, Task

class TestSimulationScenarios(unittest.TestCase):

    def test_six_week_simulation_runs_in_milliseconds(self):
        """Validates Milestone 2 exit criterion: 6 simulated weeks run in < 60s."""
        start_time = datetime(2026, 8, 20, 8, 0)
        store = FakeStore(workspace_id="ws_sim_6wk")
        
        # Seed state with multiple commitments
        store.add_commitment(Commitment(
            id="c_acme",
            workspace_id="ws_sim_6wk",
            title="Acme Client Redesign",
            kind="client",
            stake=5,
            deadline=start_time + timedelta(days=28)
        ))
        store.add_commitment(Commitment(
            id="c_course",
            workspace_id="ws_sim_6wk",
            title="ML Course Track",
            kind="course",
            stake=3,
            open_ended=True
        ))

        # Seed tasks
        for i in range(15):
            store.add_task(Task(
                id=f"t_acme_{i}",
                workspace_id="ws_sim_6wk",
                commitment_id="c_acme",
                title=f"Acme Frontend component {i}",
                estimate_minutes=60,
                status="ready"
            ))

        for i in range(8):
            store.add_task(Task(
                id=f"t_course_{i}",
                workspace_id="ws_sim_6wk",
                commitment_id="c_course",
                title=f"ML module {i}",
                estimate_minutes=45,
                status="ready"
            ))

        persona = ScriptedPersona(
            name="Dev Alex",
            overrun_multipliers={"frontend": 1.3},
            skip_slots=["before_08:00"]
        )

        t0 = datetime.now(timezone.utc)
        result = run_simulation(days=42, store=store, persona=persona, start_time=start_time)
        t1 = datetime.now(timezone.utc)
        elapsed_sec = (t1 - t0).total_seconds()

        self.assertTrue(result.passed)
        self.assertEqual(result.days_simulated, 42)
        self.assertTrue(len(result.traces) > 0)
        self.assertTrue(elapsed_sec < 5.0, f"6-week simulation took {elapsed_sec}s, expected < 5s")

if __name__ == "__main__":
    unittest.main()
