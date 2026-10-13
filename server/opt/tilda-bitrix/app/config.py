from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Tilda Bitrix Webhook"
    public_base_url: str = "http://195.133.53.59:8000"
    inspect_token: str | None = None
    database_url: str

    bitrix_webhook_url: str | None = None
    bitrix_webhook_secret: str | None = None
    responsible_user_id: str | None = None
    default_currency: str = "RUB"
    log_field_code: str = "UF_CRM_1781629422"

    bitrix_tilda_order_field: str = "UF_CRM_ORDERID"
    bitrix_form_field: str = "UF_CRM_TILDA_FORMID"
    bitrix_formname_field: str = "UF_CRM_FORMNAME"
    bitrix_amount_field: str = "UF_CRM_TILDA_AMOUNT"
    bitrix_subtotal_field: str = "UF_CRM_SUBTOTAL"
    bitrix_payment_status_field: str = "UF_CRM_PAYMENT_STATUS"
    bitrix_payment_id_field: str = "UF_CRM_PAYMENTID"
    bitrix_transaction_id_field: str = "UF_CRM_TRANSACTION_ID"
    bitrix_delivery_field: str = "UF_CRM_DELIVERY"
    bitrix_delivery_price_field: str = "UF_CRM_DELIVERYPRICE"
    bitrix_delivery_city_field: str = "UF_CRM_DELIVERY_CITY"
    bitrix_delivery_address_field: str = "UF_CRM_DELIVERYADDRE"
    bitrix_delivery_zip_field: str = "UF_CRM_DELIVERY_ZIP"
    bitrix_delivery_fio_field: str = "UF_CRM_DELIVERY_FIO"
    bitrix_delivery_comment_field: str = "UF_CRM_DELIVERY_COMMENT"
    bitrix_delivery_pickup_id_field: str = "UF_CRM_DELIVERY_PICKUP_ID"
    bitrix_product_field: str = "UF_CRM_PRODUCT"
    bitrix_products_json_field: str = "UF_CRM_PRODUCTS_JSON"
    bitrix_cookies_field: str = "UF_CRM_COOKIES"
    bitrix_checkbox_field: str = "UF_CRM_CHECKBOX"
    bitrix_checkbox2_field: str = "UF_CRM_CHECKBOX2"
    bitrix_request_id_field: str = "UF_CRM_TILDA_REQUEST_ID"
    bitrix_promocode_field: str = "UF_CRM_PROMOCODE"


settings = Settings()
