from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import firebase_admin
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from firebase_admin import _messaging_utils
from firebase_admin import credentials, firestore, messaging
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Ecommerce Notifications Backend")


class SendNotificationResponse(BaseModel):
    sale_id: str
    user_id: str
    tokens_sent: int
    responses: list[str]


def initialize_firebase() -> None:
    if firebase_admin._apps:
        return

    credentials_path = os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "serviceAccountKey.json",
    )

    resolved_path = Path(credentials_path)
    if not resolved_path.is_absolute():
        resolved_path = Path(__file__).parent / resolved_path

    if not resolved_path.exists():
        raise RuntimeError(f"Firebase credentials file not found: {resolved_path}")

    firebase_admin.initialize_app(credentials.Certificate(str(resolved_path)))


def firestore_client() -> firestore.Client:
    initialize_firebase()
    return firestore.client()


def get_purchase(sale_id: str) -> dict[str, Any]:
    snapshot = firestore_client().collection("purchases").document(sale_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Sale not found.")

    purchase = snapshot.to_dict() or {}
    purchase["id"] = snapshot.id
    return purchase


def get_user_tokens(user_id: str) -> list[str]:
    snapshot = firestore_client().collection("users").document(user_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="User not found.")

    user = snapshot.to_dict() or {}
    tokens: list[str] = []

    fcm_token = user.get("fcmToken")
    if isinstance(fcm_token, str) and fcm_token:
        tokens.append(fcm_token)

    fcm_tokens = user.get("fcmTokens")
    if isinstance(fcm_tokens, list):
        tokens.extend(token for token in fcm_tokens if isinstance(token, str) and token)

    unique_tokens = list(dict.fromkeys(tokens))
    if not unique_tokens:
        raise HTTPException(status_code=400, detail="User has no FCM tokens.")

    return unique_tokens


def delete_user_token(user_id: str, token: str) -> None:
    firestore_client().collection("users").document(user_id).update(
        {
            "fcmToken": firestore.DELETE_FIELD,
            "fcmTokens": firestore.ArrayRemove([token]),
        }
    )


def send_sale_notification(
    sale_id: str,
    purchase: dict[str, Any],
    tokens: list[str],
) -> list[str]:
    total = purchase.get("total", 0)
    user_id = str(purchase.get("userId", ""))
    title = "Compra completada"
    body = f"Tu compra por EUR {float(total):.2f} ya esta en tu historial."

    responses: list[str] = []
    for token in tokens:
        message = messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
            data={
                "route": "purchase_detail",
                "saleId": sale_id,
                "purchaseId": sale_id,
                "total": str(total),
                "userId": str(purchase.get("userId", "")),
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="ecommerce_notifications",
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                ),
            ),
        )
        try:
            responses.append(messaging.send(message))
        except _messaging_utils.UnregisteredError as error:
            delete_user_token(user_id, token)
            raise HTTPException(
                status_code=400,
                detail=(
                    "The saved FCM token is no longer registered. "
                    "Open the Flutter app again with this user to generate "
                    "and save a new token, then retry."
                ),
            ) from error

    return responses


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/notifications/sales/{sale_id}/send", response_model=SendNotificationResponse)
def send_notification_for_sale(sale_id: str) -> SendNotificationResponse:
    try:
        purchase = get_purchase(sale_id)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    user_id = str(purchase.get("userId", ""))
    if not user_id:
        raise HTTPException(status_code=400, detail="Sale has no userId.")

    try:
        tokens = get_user_tokens(user_id)
        responses = send_sale_notification(sale_id, purchase, tokens)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    return SendNotificationResponse(
        sale_id=sale_id,
        user_id=user_id,
        tokens_sent=len(tokens),
        responses=responses,
    )
