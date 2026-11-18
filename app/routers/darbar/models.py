from pydantic import BaseModel

from app.entities.darbar_ent import DarbarText


class DarbarModels:
    class V1:
        class Input:
            class SetDarbar(BaseModel):
                # At most 4,000 characters. An empty text means gems are told nothing here.
                text: DarbarText

        class Output:
            class Darbar(BaseModel):
                text: str
                # Whether it is the built-in text, which is so until the user sets their own.
                is_default: bool
