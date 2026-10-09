"""Jinja2 prompt-template registry for ForgeOS.

:class:`PromptRegistry` stores named templates and renders them with strict
variable checking: rendering with a missing variable raises
:class:`PromptRenderError` (a :class:`KeyError`) naming the variable, and
rendering an unknown template raises :class:`KeyError` listing the templates
that *are* registered.

Four marketing templates are registered on the module-level
:obj:`default_registry`: ``email_subject``, ``email_body``, ``sms_body`` and
``social_post``.
"""

from __future__ import annotations

from typing import Any

import jinja2


class PromptRenderError(KeyError):
    """A template could not be rendered (missing variable or bad syntax)."""


class PromptRegistry:
    """Named Jinja2 templates with strict undefined-variable checking."""

    def __init__(self) -> None:
        self._env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self._templates: dict[str, jinja2.Template] = {}
        self._sources: dict[str, str] = {}
        self._defaults: dict[str, dict[str, Any]] = {}

    def register(
        self, name: str, template: str, defaults: dict[str, Any] | None = None
    ) -> None:
        """Register ``template`` under ``name``.

        ``defaults`` supplies fallback values merged *under* the variables
        passed to :meth:`render`. Templates are compiled eagerly so syntax
        errors surface here, not at render time.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Template name must be a non-empty string")
        if not isinstance(template, str):
            raise TypeError(
                f"Template source for {name!r} must be a string, "
                f"got {type(template).__name__}"
            )
        try:
            compiled = self._env.from_string(template)
        except jinja2.TemplateError as exc:
            raise ValueError(
                f"Invalid Jinja2 template registered as {name!r}: {exc}"
            ) from exc
        key = name.strip()
        self._templates[key] = compiled
        self._sources[key] = template
        self._defaults[key] = dict(defaults or {})

    @property
    def names(self) -> list[str]:
        """Registered template names, sorted."""
        return sorted(self._templates)

    def __contains__(self, name: object) -> bool:
        return name in self._templates

    def render(self, name: str, variables: dict[str, Any]) -> str:
        """Render template ``name`` with ``variables``.

        Raises:
            KeyError: if ``name`` is not registered (message lists available names).
            PromptRenderError: if a variable the template needs is missing, or the
                template fails to render.
        """
        template = self._templates.get(name)
        if template is None:
            available = ", ".join(self.names) or "(none registered)"
            raise KeyError(
                f"Unknown prompt template {name!r}. Available templates: {available}"
            )
        merged: dict[str, Any] = {**self._defaults[name], **variables}
        try:
            return template.render(merged)
        except jinja2.UndefinedError as exc:
            raise PromptRenderError(
                f"Prompt template {name!r} is missing required variable: {exc}"
            ) from exc
        except jinja2.TemplateError as exc:
            raise PromptRenderError(
                f"Prompt template {name!r} failed to render: {exc}"
            ) from exc


def _builtin_templates() -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        "email_subject": (
            "{{ offer_headline }} — from {{ brand_name }}",
            {"brand_name": "Forge"},
        ),
        "email_body": (
            "Hi {{ first_name }},\n"
            "\n"
            "{{ intro }}\n"
            "\n"
            "{{ offer_details }}\n"
            "\n"
            "{{ cta_text }}: {{ cta_url }}\n"
            "\n"
            "{{ sign_off }},\n"
            "{{ brand_name }}\n"
            "\n"
            "---\n"
            "You're receiving this because you subscribed. "
            "Unsubscribe anytime: {{ unsubscribe_url }}",
            {
                "first_name": "there",
                "sign_off": "Best regards",
                "brand_name": "Forge",
            },
        ),
        "sms_body": (
            "{{ brand_name }}: {{ message }} Reply STOP to opt out. {{ short_url }}",
            {"brand_name": "Forge", "short_url": ""},
        ),
        "social_post": (
            "{{ hook }}\n\n{{ body }}\n\n{{ hashtags }}",
            {"hashtags": ""},
        ),
    }


BUILTIN_TEMPLATE_NAMES: tuple[str, ...] = tuple(_builtin_templates())

default_registry = PromptRegistry()
for _name, (_source, _defaults) in _builtin_templates().items():
    default_registry.register(_name, _source, _defaults)


def render(name: str, variables: dict[str, Any]) -> str:
    """Render one of the built-in templates from :obj:`default_registry`."""
    return default_registry.render(name, variables)
