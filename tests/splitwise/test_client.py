"""``SplitwiseClient`` async wrapper sanity checks.

We don't hit the real network — ``fake_sdk`` patches ``splitwise.Splitwise``
in both call sites (client.py and oauth.py).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.splitwise.client import SplitwiseAPIError, SplitwiseClient
from app.splitwise.models import Split

from .conftest import FakeSDKExpense, FakeSDKGroup, FakeSDKUser, FakeSplitwiseSDK


@pytest.mark.asyncio
async def test_constructor_sets_oauth2_token(fake_sdk: type[FakeSplitwiseSDK]) -> None:
    SplitwiseClient("the-access-token")
    sdk = FakeSplitwiseSDK.instances[-1]
    # Constructor must call setOAuth2AccessToken with the right shape.
    set_calls = [c for c in sdk.calls if c[0] == "setOAuth2AccessToken"]
    assert len(set_calls) == 1
    assert set_calls[0][1][0] == {
        "access_token": "the-access-token",
        "token_type": "bearer",
    }


def test_constructor_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        SplitwiseClient("")


@pytest.mark.asyncio
async def test_get_current_user(fake_sdk: type[FakeSplitwiseSDK]) -> None:
    c = SplitwiseClient("tok")
    sdk = FakeSplitwiseSDK.instances[-1]
    sdk.current_user = FakeSDKUser(42, "Prem", "Patel", "p@example.test")

    user = await c.get_current_user()
    assert user.id == 42
    assert user.first_name == "Prem"
    assert user.email == "p@example.test"
    assert ("getCurrentUser", (), {}) in sdk.calls


@pytest.mark.asyncio
async def test_get_friends(fake_sdk: type[FakeSplitwiseSDK]) -> None:
    c = SplitwiseClient("tok")
    sdk = FakeSplitwiseSDK.instances[-1]
    sdk.friends = [FakeSDKUser(1, "A"), FakeSDKUser(2, "B")]

    friends = await c.get_friends()
    assert [f.id for f in friends] == [1, 2]
    assert ("getFriends", (), {}) in sdk.calls


@pytest.mark.asyncio
async def test_get_groups(fake_sdk: type[FakeSplitwiseSDK]) -> None:
    c = SplitwiseClient("tok")
    sdk = FakeSplitwiseSDK.instances[-1]
    sdk.groups = [
        FakeSDKGroup(10, "Roommates", [FakeSDKUser(1), FakeSDKUser(2)]),
        FakeSDKGroup(20, "Trip", [FakeSDKUser(3)]),
    ]

    groups = await c.get_groups()
    assert [(g.id, g.name, g.member_ids) for g in groups] == [
        (10, "Roommates", [1, 2]),
        (20, "Trip", [3]),
    ]


@pytest.mark.asyncio
async def test_create_expense_calls_sdk_with_balanced_splits(
    fake_sdk: type[FakeSplitwiseSDK],
) -> None:
    c = SplitwiseClient("tok")
    sdk = FakeSplitwiseSDK.instances[-1]
    sdk.create_expense_result = (FakeSDKExpense(9001), None)

    eid = await c.create_expense(
        cost=Decimal("20.00"),
        currency="USD",
        description="dinner",
        group_id=42,
        splits=[
            Split(
                splitwise_user_id=1,
                paid_share=Decimal("20.00"),
                owed_share=Decimal("10.00"),
            ),
            Split(
                splitwise_user_id=2,
                paid_share=Decimal("0"),
                owed_share=Decimal("10.00"),
            ),
        ],
    )
    assert eid == 9001

    # Pull the Expense object we handed to createExpense and verify it.
    create_calls = [c for c in sdk.calls if c[0] == "createExpense"]
    assert len(create_calls) == 1
    expense_arg = create_calls[0][1][0]
    assert expense_arg.getCost() == "20.00"
    assert expense_arg.getCurrencyCode() == "USD"
    assert expense_arg.getDescription() == "dinner"
    assert expense_arg.getGroupId() == 42
    users = expense_arg.getUsers()
    assert len(users) == 2
    assert {u.getId() for u in users} == {1, 2}


@pytest.mark.asyncio
async def test_create_expense_propagates_sdk_errors(
    fake_sdk: type[FakeSplitwiseSDK],
) -> None:
    c = SplitwiseClient("tok")
    sdk = FakeSplitwiseSDK.instances[-1]
    sdk.create_expense_result = (None, {"base": ["bad split"]})

    with pytest.raises(SplitwiseAPIError):
        await c.create_expense(
            cost=Decimal("1.00"),
            currency="USD",
            description="x",
            group_id=None,
            splits=[
                Split(
                    splitwise_user_id=1,
                    paid_share=Decimal("1.00"),
                    owed_share=Decimal("1.00"),
                )
            ],
        )


@pytest.mark.asyncio
async def test_create_expense_rejects_empty_splits(
    fake_sdk: type[FakeSplitwiseSDK],
) -> None:
    c = SplitwiseClient("tok")
    with pytest.raises(ValueError):
        await c.create_expense(
            cost=Decimal("1.00"),
            currency="USD",
            description="x",
            group_id=None,
            splits=[],
        )
