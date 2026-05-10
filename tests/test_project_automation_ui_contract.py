from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectAutomationUiContractTests(unittest.TestCase):
    def test_weekday_mapping_matches_backend_contract(self) -> None:
        contents = (
            ROOT / "admin-ui" / "components" / "tenant-project-discord-page.tsx"
        ).read_text(encoding="utf-8")

        expected_weekdays = [
            '{ value: 0, label: "Mon" }',
            '{ value: 1, label: "Tue" }',
            '{ value: 2, label: "Wed" }',
            '{ value: 3, label: "Thu" }',
            '{ value: 4, label: "Fri" }',
            '{ value: 5, label: "Sat" }',
            '{ value: 6, label: "Sun" }',
        ]
        for weekday in expected_weekdays:
            self.assertIn(weekday, contents)

        self.assertIn(
            "days_of_week: kind === PROJECT_AUTOMATION_KIND_STANDUP ? [0, 1, 2, 3, 4] : [4],",
            contents,
        )


if __name__ == "__main__":
    unittest.main()
