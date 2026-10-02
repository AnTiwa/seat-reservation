from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Enter your username here (it will be passed as a Bearer token). E.g. 'alice'.",
)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> str:
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="missing authorization",
        )

    token = credentials.credentials.strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="invalid authorization",
        )

    # Exercise authentication model:
    #
    # Bearer user-123
    #
    # In production this would validate a JWT/session against
    # an identity provider.
    return token
