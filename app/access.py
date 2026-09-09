def admin_ids(cfg):
    return {cfg.admin_id} | {
        int(item.strip()) for item in getattr(cfg, "admin_ids", "").split(",") if item.strip()
    }


def is_admin(cfg, user_id):
    return user_id in admin_ids(cfg)
