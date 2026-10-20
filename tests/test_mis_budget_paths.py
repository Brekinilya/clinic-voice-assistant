"""T05: ни один путь к МИС не обходит общий бюджет — проверка по исходному коду."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
# Настоящая МИС: только с общим бюджетом PostgreSQL.
LIVE = {
    "backend.py",
    "dialogue/live.py",
    "scheduling/cli.py",
    "recovery/__main__.py",
    "handoffs/cli.py",
}
# Эмулятор через MockTransport: бюджет стенда не расходуется.
EMULATED = {"demo.py", "dialogue/bootstrap.py"}
# Прочие HTTP-клиенты приложения не ходят в МИС.
OTHER_CLIENTS = {"voice/telephony.py"}


def modules():
    for path in sorted(APP.rglob("*.py")):
        yield path.relative_to(APP).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def calls(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            label = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if label == name:
                yield node


def test_only_request_method_touches_the_mis_http_client():
    tree = ast.parse((APP / "mis/http.py").read_text(encoding="utf-8"))
    users = set()
    for function in ast.walk(tree):
        if isinstance(function, ast.AsyncFunctionDef | ast.FunctionDef):
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "_client"
                    and node.attr != "headers"
                ):
                    users.add(function.name)
    assert users == {"_request"}


def test_request_reserves_budget_before_every_send():
    tree = ast.parse((APP / "mis/http.py").read_text(encoding="utf-8"))
    request = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_request"
    )
    source = ast.unparse(request)
    send = source.index("self._client.request(")
    assert "self._budget.reserve()" in source[:send]
    assert "lease.take(write)" in source[:send]


def test_every_mis_adapter_gets_a_budget_that_matches_its_transport():
    found = {}
    for name, tree in modules():
        for call in calls(tree, "HttpMedicalSystem"):
            assert len(call.args) >= 2, name
            budget = call.args[1]
            assert isinstance(budget, ast.Call), name
            found.setdefault(name, set()).add(ast.unparse(budget.func))
    assert set(found) == LIVE | EMULATED
    assert all(found[name] == {"PostgresBudget"} for name in LIVE)
    assert all(found[name] == {"EmulatorBudget"} for name in EMULATED)


def test_live_client_is_created_only_for_budgeted_adapters():
    users = {name for name, tree in modules() if any(calls(tree, "live_client"))} - {"mis/http.py"}
    assert users == LIVE


def test_other_http_clients_never_point_at_the_mis():
    for name, tree in modules():
        for call in calls(tree, "AsyncClient"):
            keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
            if name == "mis/http.py":
                assert "mis_base_url" in keywords["base_url"]
            elif name in EMULATED:
                assert "transport" in keywords and "emulator" in keywords["transport"], name
            else:
                assert name in OTHER_CLIENTS, name
                assert "mis" not in keywords.get("base_url", ""), name
