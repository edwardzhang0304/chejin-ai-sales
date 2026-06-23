from app.core.database import Base, SessionLocal, engine
from app.models import *  # noqa: F401,F403
from app.models.sales import Sales


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.query(Sales).count()
        if existing:
            print(f"sales already seeded: {existing}")
            return
        db.add_all(
            [
                Sales(sales_name="张伟", phone="13800001001", wechat="zhangwei_car", enabled=True, sort_order=10, remark="一店销售"),
                Sales(sales_name="王敏", phone="13800001002", wechat="wangmin_car", enabled=True, sort_order=20, remark="二店销售"),
                Sales(sales_name="李强", phone="13800001003", wechat="liqiang_car", enabled=False, sort_order=30, remark="停用示例"),
            ]
        )
        db.commit()
        print("seeded sales: 张伟, 王敏, 李强")
    finally:
        db.close()


if __name__ == "__main__":
    main()
