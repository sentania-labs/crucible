"""UI routes and actions frozen from origin/main before FDY-0167."""

import ast
import inspect

from fastapi.routing import APIRoute

from crucible.adapters.ui.router import action, router

FROZEN_ROUTES = [
    ("GET", "/ui"),
    ("GET", "/ui/audit"),
    ("GET", "/ui/bootstrap"),
    ("GET", "/ui/bootstrap/{import_id}"),
    ("GET", "/ui/credentials"),
    ("GET", "/ui/credentials/{harness}/login"),
    ("GET", "/ui/gateway"),
    ("GET", "/ui/github"),
    ("GET", "/ui/github/callback"),
    ("GET", "/ui/github/installed"),
    ("GET", "/ui/harnesses"),
    ("GET", "/ui/images"),
    ("GET", "/ui/repositories"),
    ("GET", "/ui/retention"),
    ("GET", "/ui/routing"),
    ("GET", "/ui/settings"),
    ("GET", "/ui/sign-in"),
    ("GET", "/ui/tasks"),
    ("GET", "/ui/tasks/{task_id}"),
    ("GET", "/ui/tokens"),
    ("GET", "/ui/wakes"),
    ("GET", "/ui/workers"),
    ("GET", "/ui/workers/{attempt_id}/logs"),
    ("POST", "/ui/actions/{action}"),
    ("POST", "/ui/sign-in"),
    ("POST", "/ui/sign-out"),
    ("POST", "/ui/tasks/{task_id}/decisions"),
]

FROZEN_ACTIONS = [
    "bootstrap-commit",
    "bootstrap-discard",
    "command-timeout",
    "credential",
    "gate-classes",
    "gateway-models",
    "gateway-save",
    "gateway-test",
    "github-add-repository",
    "github-check",
    "github-create-app",
    "github-external-url",
    "harness",
    "harness-test",
    "hermes-limits",
    "image-promote",
    "image-rollback",
    "kubernetes-egress",
    "kubernetes-timeouts",
    "login-cancel",
    "login-code",
    "login-finish",
    "login-start",
    "policy-upload",
    "repository-register",
    "repository-remove",
    "routing-clear",
    "routing-preference",
    "routing-upload",
    "token-create",
    "token-rename",
    "token-revoke",
]


def test_ui_routes_frozen() -> None:
    assert (
        sorted(
            (method, route.path)
            for route in router.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        )
        == FROZEN_ROUTES
    )


def test_ui_actions_frozen() -> None:
    tree = ast.parse(inspect.getsource(action))
    accepted = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "action"
        ):
            value = node.comparators[0]
            if isinstance(value, ast.Constant):
                accepted.add(value.value)
            elif isinstance(value, ast.Tuple):
                accepted.update(item.value for item in value.elts if isinstance(item, ast.Constant))
    assert accepted == set(FROZEN_ACTIONS)
