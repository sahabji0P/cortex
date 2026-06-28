from datetime import datetime

from pydantic import BaseModel, EmailStr


class UserModels:
    class V1:
        class Input:
            class Register(BaseModel):
                email: EmailStr
                password: str

            class Login(BaseModel):
                email: EmailStr
                password: str

        class Output:
            class User(BaseModel):
                id: str
                email: str
                is_active: bool
                created_at: datetime

            class Token(BaseModel):
                access_token: str
                token_type: str = "bearer"
