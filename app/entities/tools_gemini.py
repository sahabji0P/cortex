from google.genai import types


class GeminiToolsEntityClass:
    @staticmethod
    def google_search() -> types.Tool:
        """Build a Gemini Google Search grounding tool."""
        return types.Tool(google_search=types.GoogleSearch())

    @staticmethod
    def code_execution() -> types.Tool:
        """Build a Gemini code execution tool."""
        return types.Tool(code_execution=types.ToolCodeExecution())

    @staticmethod
    def url_context() -> types.Tool:
        """Build a Gemini URL context tool."""
        return types.Tool(url_context=types.UrlContext())

    @staticmethod
    def builtins() -> list[types.Tool]:
        """Build the default Gemini built-in tool set for experiments."""
        return [
            GeminiToolsEntityClass.google_search(),
            GeminiToolsEntityClass.code_execution(),
            GeminiToolsEntityClass.url_context(),
        ]
