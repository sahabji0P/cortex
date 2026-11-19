from pydantic import BaseModel

from app.adapters.openai_adp import OpenAIVia


class ProvidersModels:
    class V1:
        class Input:
            class SetOpenAIRoute(BaseModel):
                via: OpenAIVia

        class Output:
            class OpenAIRoute(BaseModel):
                # "api": straight to OpenAI with the API key. "proxy": through the proxy.
                via: OpenAIVia
                # Whether each way is set up in the backend's settings.
                api: bool
                proxy: bool
