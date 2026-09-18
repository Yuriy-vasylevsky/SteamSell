from sqlalchemy import select

from app.models import User


def admin_ids(cfg):
    return {cfg.admin_id} | {
        int(item.strip()) for item in getattr(cfg, "admin_ids", "").split(",") if item.strip()
    }


def is_primary_admin(cfg, user_id):
    return user_id == cfg.admin_id


def is_admin(cfg, user_id, user=None):
    return user_id in admin_ids(cfg) or bool(user and user.is_admin)


async def all_admin_ids(session, cfg):
    database_ids = await session.scalars(select(User.id).where(User.is_admin.is_(True)))
    return admin_ids(cfg) | set(database_ids)
