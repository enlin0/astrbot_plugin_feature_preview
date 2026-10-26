"""Feature preview plugin: list installed AstrBot plugins and their descriptions."""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star

DEFAULT_TRIGGER_WORDS = [
    "你可以做什么",
    "你能做什么",
    "你会什么",
    "你有什么功能",
    "你有哪些功能",
    "有什么功能",
    "有什么插件",
    "插件列表",
    "功能列表",
]
DEFAULT_ITEM_FORMAT = "{index}. {name}{status}：{desc}"
DEFAULT_HEADER = "本 Bot 目前已安装这些插件："
DEFAULT_FOOTER = "共 {count} 个插件。"
DEFAULT_EMPTY_REPLY = "暂时没有可以展示的插件。"
DEFAULT_NO_DESC = "暂无简介"

# Characters removed when comparing trigger words in contains/exact mode.
_NOISE_PATTERN = re.compile(r"[\s\u3000，。！？!?,、；;：:~～…]+")
_REGEX_CACHE: dict[tuple[str, ...], list[re.Pattern[str]]] = {}


def _as_bool(value: Any, default: bool) -> bool:
    """Convert loosely typed config values to bool.

    Args:
        value: Raw config value.
        default: Fallback when the value cannot be interpreted.

    Returns:
        The parsed boolean, or the fallback.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled", "开", "开启", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "关", "关闭", "否"}:
        return False
    return default


def _as_int(value: Any, default: int, minimum: int | None = None) -> int:
    """Convert a config value to int with an optional lower bound.

    Args:
        value: Raw config value.
        default: Fallback when parsing fails.
        minimum: Lower bound applied after parsing.

    Returns:
        The parsed integer.
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and number < minimum:
        return minimum
    return number


def _as_list(value: Any) -> list[str]:
    """Normalize a config value into a list of non-empty strings.

    Args:
        value: A list, a comma/newline separated string, or a single value.

    Returns:
        Normalized string list.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[\n,，;；、]+", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize(text: str) -> str:
    """Strip whitespace and punctuation and fold case for trigger matching.

    Args:
        text: Raw text to normalize.

    Returns:
        Normalized text.
    """
    if not isinstance(text, str):
        return ""
    return _NOISE_PATTERN.sub("", text).casefold()


def _clean_text(text: Any, max_length: int) -> str:
    """Collapse whitespace and optionally truncate text for display.

    Args:
        text: Raw text.
        max_length: Maximum length, or 0 for no limit.

    Returns:
        Cleaned single-line text.
    """
    if not isinstance(text, str):
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if max_length > 0 and len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip() + "…"
    return cleaned


class _SafeDict(dict):
    """Format mapping that renders unknown placeholders as empty strings."""

    def __missing__(self, key: str) -> str:
        return ""


def _safe_format(template: str, values: dict[str, Any]) -> str:
    """Format a template without raising on unknown or malformed placeholders.

    Args:
        template: Template string using ``str.format`` placeholders.
        values: Values available to the template.

    Returns:
        Formatted text, or the original template on failure.
    """
    try:
        return template.format_map(_SafeDict(values))
    except (KeyError, IndexError, ValueError):
        return template


class FeaturePreviewPlugin(Star):
    """List installed plugins and answer "what can you do" style questions."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        """Initialize the plugin.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration loaded from ``_conf_schema.json``.
        """
        super().__init__(context)
        self.config = config or {}
        logger.info("Feature preview plugin loaded.")

    def _get(self, key: str, default: Any) -> Any:
        """Read a config value, falling back to the default when missing.

        Args:
            key: Config key.
            default: Fallback value.

        Returns:
            The configured value or the fallback.
        """
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _star_tokens(self, metadata: Any) -> set[str]:
        """Collect the identifiers a whitelist/blacklist entry may use.

        Args:
            metadata: A ``StarMetadata`` instance.

        Returns:
            Lower-cased identifier set.
        """
        tokens = {
            str(getattr(metadata, "name", "") or ""),
            str(getattr(metadata, "display_name", "") or ""),
            str(getattr(metadata, "root_dir_name", "") or ""),
            str(getattr(metadata, "plugin_id", "") or ""),
            str(getattr(metadata, "module_path", "") or ""),
        }
        aliases = set(tokens)
        for token in tokens:
            token = token.strip()
            if token.lower().startswith("astrbot_plugin_"):
                aliases.add(token[len("astrbot_plugin_") :])
        return {token.strip().casefold() for token in aliases if token.strip()}

    def _matches_entries(self, metadata: Any, entries: list[str]) -> bool:
        """Check whether a plugin matches any whitelist/blacklist entry.

        Args:
            metadata: A ``StarMetadata`` instance.
            entries: Configured names, display names, or directory names.

        Returns:
            True when one entry matches the plugin.
        """
        tokens = self._star_tokens(metadata)
        return any(str(entry).strip().casefold() in tokens for entry in entries)

    def _select_stars(self) -> list[Any]:
        """Select the plugins to display according to the config.

        Returns:
            Filtered and sorted plugin metadata list.
        """
        mode = str(self._get("display_mode", "auto")).strip().lower()
        whitelist = _as_list(self._get("whitelist", []))
        blacklist = _as_list(self._get("blacklist", []))
        show_disabled = _as_bool(self._get("show_disabled", False), False)
        show_builtin = _as_bool(self._get("show_builtin", False), False)
        include_self = _as_bool(self._get("include_self", False), False)

        try:
            stars = list(self.context.get_all_stars() or [])
        except Exception:
            logger.exception("Failed to read the plugin registry.")
            return []

        selected = []
        for metadata in stars:
            if metadata is None:
                continue
            if not include_self and getattr(metadata, "star_cls", None) is self:
                continue
            if getattr(metadata, "reserved", False) and not show_builtin:
                continue
            if not getattr(metadata, "activated", True) and not show_disabled:
                continue
            if (
                mode == "whitelist"
                and whitelist
                and not self._matches_entries(metadata, whitelist)
            ):
                continue
            if blacklist and self._matches_entries(metadata, blacklist):
                continue
            selected.append(metadata)

        selected.sort(
            key=lambda item: (
                str(getattr(item, "display_name", "") or getattr(item, "name", ""))
                .strip()
                .casefold()
            ),
        )

        max_plugins = _as_int(self._get("max_plugins", 30), 30, 0)
        if max_plugins > 0:
            selected = selected[:max_plugins]
        return selected

    def _build_reply(self) -> str:
        """Build the plugin list reply text.

        Returns:
            A plain-text reply listing the selected plugins.
        """
        stars = self._select_stars()
        if not stars:
            return str(self._get("empty_reply", DEFAULT_EMPTY_REPLY)).strip()

        item_format = str(self._get("item_format", DEFAULT_ITEM_FORMAT))
        max_desc_length = _as_int(self._get("max_desc_length", 120), 120, 0)

        lines = []
        for index, metadata in enumerate(stars, start=1):
            display_name = (
                str(
                    getattr(metadata, "display_name", "")
                    or getattr(metadata, "name", "")
                    or getattr(metadata, "root_dir_name", "")
                    or "未知插件",
                ).strip()
                or "未知插件"
            )
            description = _clean_text(
                getattr(metadata, "desc", "") or getattr(metadata, "short_desc", ""),
                max_desc_length,
            )
            values = {
                "index": index,
                "name": display_name,
                "desc": description or DEFAULT_NO_DESC,
                "plugin_name": str(getattr(metadata, "name", "") or display_name),
                "id": str(getattr(metadata, "plugin_id", "") or ""),
                "version": str(getattr(metadata, "version", "") or ""),
                "author": str(getattr(metadata, "author", "") or ""),
                "status": "" if getattr(metadata, "activated", True) else "（已停用）",
            }
            line = _safe_format(item_format, values).strip()
            lines.append(line or f"{index}. {display_name}：{values['desc']}")

        header = _safe_format(
            str(self._get("reply_header", DEFAULT_HEADER)),
            {"count": len(lines)},
        ).strip()
        footer = _safe_format(
            str(self._get("reply_footer", DEFAULT_FOOTER)),
            {"count": len(lines)},
        ).strip()
        return "\n".join(part for part in (header, "\n".join(lines), footer) if part)

    def _match_trigger(self, text: str) -> bool:
        """Check whether a message matches one of the configured trigger words.

        Args:
            text: Incoming message text.

        Returns:
            True when the message should trigger the plugin list reply.
        """
        words = _as_list(self._get("trigger_words", DEFAULT_TRIGGER_WORDS))
        if not words:
            return False
        mode = str(self._get("match_mode", "contains")).strip().lower()

        if mode == "regex":
            key = tuple(words)
            patterns = _REGEX_CACHE.get(key)
            if patterns is None:
                patterns = []
                for pattern in words:
                    try:
                        patterns.append(re.compile(pattern, re.IGNORECASE))
                    except re.error as exc:
                        logger.warning(
                            "Ignoring invalid trigger regex %r: %s",
                            pattern,
                            exc,
                        )
                if len(_REGEX_CACHE) >= 32:
                    _REGEX_CACHE.clear()
                _REGEX_CACHE[key] = patterns
            return any(pattern.search(text or "") for pattern in patterns)

        normalized_text = _normalize(text)
        if not normalized_text:
            return False
        normalized_words = [_normalize(word) for word in words]
        if mode == "exact":
            return normalized_text in normalized_words
        return any(word and word in normalized_text for word in normalized_words)

    async def _reply_with_list(self, event: AstrMessageEvent):
        """Build the reply and yield it as a plain-text result.

        Args:
            event: Incoming message event.
        """
        try:
            yield event.plain_result(self._build_reply())
        except Exception:
            logger.exception("Failed to build the plugin list reply.")
            yield event.plain_result("唔……读取插件列表的时候出错了。")

    @filter.command("功能", alias={"功能预览"})
    async def feature_preview_command(self, event: AstrMessageEvent):
        """查看本 Bot 已安装插件的功能清单。"""
        if not _as_bool(self._get("enabled", True), True):
            return
        async for result in self._reply_with_list(event):
            yield result

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听消息，在命中触发词时回复已安装插件的名称与简介。"""
        try:
            if not _as_bool(self._get("enabled", True), True):
                return
            text = event.message_str or ""
            if not text.strip():
                return
            sender_id = event.get_sender_id()
            self_id = event.get_self_id()
            if sender_id and self_id and sender_id == self_id:
                return
            if not event.is_private_chat() and _as_bool(
                self._get("require_at_in_group", False),
                False,
            ):
                if not event.is_at_or_wake_command:
                    return
            if not self._match_trigger(text):
                return
            logger.debug("Feature preview triggered by: %s", text[:80])
            async for result in self._reply_with_list(event):
                yield result
        except Exception:
            logger.exception("Feature preview failed to handle the message.")

    async def terminate(self):
        """Release runtime state when the plugin is unloaded."""
        _REGEX_CACHE.clear()
