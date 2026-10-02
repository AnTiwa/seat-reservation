from fastapi import Header, HTTPException


def get_current_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="missing authorization",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="invalid authorization",
        )

    token = authorization.removeprefix("Bearer ").strip()

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
