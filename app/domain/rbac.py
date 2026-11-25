"""Role-based access control: one permission map shared by the Streamlit UI and the REST API."""
ROLES: dict[str, frozenset[str]] = {
    "ADMIN": frozenset({"*"}),
    "FINANCE": frozenset({"dashboard.read", "residents.read", "finance.read", "finance.write",
                          "inventory.read", "procurement.read", "recommendations.read"}),
    "NURSE": frozenset({"dashboard.read", "residents.read", "residents.write", "care.write", "inventory.read"}),
    "HR": frozenset({"dashboard.read", "hr.read", "hr.write", "recommendations.read"}),
    "PROCUREMENT": frozenset({"dashboard.read", "inventory.read", "inventory.write",
                              "procurement.read", "procurement.write"}),
    "VIEWER": frozenset({"dashboard.read", "residents.read"}),
}


def can(role: str, permission: str) -> bool:
    perms = ROLES.get(role, frozenset())
    return "*" in perms or permission in perms


def valid_role(role: str) -> bool:
    return role in ROLES
