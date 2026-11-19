from typing import Literal

from pydantic import BaseModel

from app.adapters.openai_adp import OpenAIVia


class ProvidersModels:
    class V1:
        class Input:
            class SetOpenAIRoute(BaseModel):
                via: OpenAIVia

        class Output:
            class Account(BaseModel):
                """One ChatGPT login the proxy holds."""

                email: str
                # The ChatGPT plan, such as "free", "plus" or "team". Null when not known.
                plan: str | None
                # False when the proxy cannot use it, as when its pass was refused.
                active: bool
                # What is wrong with it, in the proxy's words. Null when nothing is.
                problem: str | None

            class OpenAIRoute(BaseModel):
                # "api": straight to OpenAI with the API key. "proxy": through the proxy.
                via: OpenAIVia
                # Whether each way is set up in the backend's settings.
                api: bool
                proxy: bool
                # Whether the proxy's ChatGPT account can be seen and changed from here.
                manageable: bool
                # The proxy's ChatGPT accounts. Null when not known: not manageable, or
                # the proxy did not answer.
                accounts: list["ProvidersModels.V1.Output.Account"] | None

            class SignInStarted(BaseModel):
                # The address to open in the browser. It comes back to the proxy.
                url: str
                # What to ask about this sign-in by.
                state: str

            class SignIn(BaseModel):
                # "wait" until the user finishes in the browser, then "ok" or "error".
                status: Literal["wait", "ok", "error"]
                # Why it failed. Null unless `status` is "error".
                reason: str | None
                # The account signed in with. Null until `status` is "ok".
                email: str | None
