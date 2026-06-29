"""
app.py - Hovedapplikasjon for VM 2026 tipping.
Kjør med: python app.py
"""
import os
import json
from datetime import datetime
import pytz
from flask import Flask, redirect, url_for
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from models import (
    db,
    CompetitionSetting,
    ScoreSetting,
    Group,
    Team,
    Match,
    BracketEdge,
    ThirdPlaceAssignmentRule,
)
from routes_user import user_bp
from routes_admin import admin_bp

OSLO_TZ = pytz.timezone("Europe/Oslo")
APP_VERSION = "1.15"


def create_app():
    app = Flask(__name__)

    # Config
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")
    if app.secret_key == "dev-secret-key-change-this":
        print("WARNING: SECRET_KEY is using the default value. Set SECRET_KEY in the environment for production.")
    db_path = os.path.join(os.path.dirname(__file__), "instance", "app.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit

    db.init_app(app)

    app.register_blueprint(user_bp)
    app.register_blueprint(admin_bp)

    # Jinja2 filters and globals
    @app.template_filter("oslo_time")
    def oslo_time_filter(dt):
        if dt is None:
            return ""
        if not hasattr(dt, "tzinfo") or dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(OSLO_TZ).strftime("%d.%m.%Y")

    @app.template_filter("oslo_datetime")
    def oslo_datetime_filter(dt):
        if dt is None:
            return ""
        if not hasattr(dt, "tzinfo") or dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(OSLO_TZ).strftime("%d.%m.%Y %H:%M")

    @app.template_filter("round_label")
    def round_label_filter(round_name):
        labels = {
            "group_stage": "Gruppespill",
            "round_of_32": "Round of 32",
            "round_of_16": "Åttendedelsfinale",
            "quarter_final": "Kvartfinale",
            "semi_final": "Semifinale",
            "third_place": "Bronsefinale",
            "final": "Finale",
        }
        return labels.get(round_name, round_name)

    @app.template_global()
    def now_oslo():
        return datetime.now(OSLO_TZ).strftime("%d.%m.%Y")

    @app.context_processor
    def inject_app_version():
        return {"app_version": APP_VERSION}

    with app.app_context():
        db.create_all()
        _ensure_users_has_paid()
        _seed_defaults()

    return app


def _seed_defaults():
    """Seed default competition settings, score settings, and bracket edges."""
    # Competition settings
    defaults_comp = {
        "competition_name": "VM 2026 Tipping",
        "entry_fee": "250",
        "prize_pool": "0",
        "show_public_statistics": "1",
        "show_scoreboard": "1",
    }
    for k, v in defaults_comp.items():
        if not CompetitionSetting.query.filter_by(key=k).first():
            db.session.add(CompetitionSetting(key=k, value=v))

    # Score settings
    from scoring import DEFAULT_SCORES
    for k, v in DEFAULT_SCORES.items():
        if not ScoreSetting.query.filter_by(key=k).first():
            db.session.add(ScoreSetting(key=k, value=str(v)))

    # Bracket edges
    _seed_bracket_edges()
    _seed_third_place_assignment_rules()

    # Normalize match lock times to be per-match (1h before kickoff)
    from datetime import timedelta
    changed = False
    for m in Match.query.filter(Match.phase.in_(["group", "knockout"])).all():
        if m.kickoff_at_utc:
            expected_lock = m.kickoff_at_utc - timedelta(hours=1)
            if m.lock_at_utc != expected_lock:
                m.lock_at_utc = expected_lock
                changed = True

    if changed:
        first_group = Match.query.filter_by(phase="group").order_by(Match.kickoff_at_utc).first()
        if first_group and first_group.kickoff_at_utc:
            CompetitionSetting.set(
                "group_stage_lock_at",
                (first_group.kickoff_at_utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            )

    db.session.commit()


def _seed_third_place_assignment_rules():
    """Seed third-place assignment rules from JSON data file."""
    rules_path = os.path.join(os.path.dirname(__file__), "data", "third_place_assignment_rules.json")
    if not os.path.exists(rules_path):
        print(f"WARNING: Missing rules file: {rules_path}")
        return

    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = json.load(f)
    except Exception as exc:
        print(f"WARNING: Could not load third-place rules: {exc}")
        return

    changed = False
    for row in rules:
        key = (row.get("qualified_groups_key") or "").strip()
        if not key:
            continue

        existing = ThirdPlaceAssignmentRule.query.filter_by(qualified_groups_key=key).first()
        if existing:
            continue

        db.session.add(ThirdPlaceAssignmentRule(
            qualified_groups_key=key,
            match_74_group=row.get("match_74_group"),
            match_77_group=row.get("match_77_group"),
            match_79_group=row.get("match_79_group"),
            match_80_group=row.get("match_80_group"),
            match_81_group=row.get("match_81_group"),
            match_82_group=row.get("match_82_group"),
            match_85_group=row.get("match_85_group"),
            match_87_group=row.get("match_87_group"),
        ))
        changed = True

    if changed:
        db.session.commit()


def _seed_bracket_edges():
    """Seed bracket flow edges if not already present."""
    edges = [
        # Round of 32 → Round of 16
        (73, "winner", 90, "away"),
        (74, "winner", 89, "home"),
        (75, "winner", 90, "home"),
        (76, "winner", 91, "home"),
        (77, "winner", 89, "away"),
        (78, "winner", 91, "away"),
        (79, "winner", 92, "home"),
        (80, "winner", 92, "away"),
        (81, "winner", 94, "home"),
        (82, "winner", 94, "away"),
        (83, "winner", 93, "home"),
        (84, "winner", 93, "away"),
        (85, "winner", 96, "home"),
        (86, "winner", 95, "home"),
        (87, "winner", 96, "away"),
        (88, "winner", 95, "away"),
        # Round of 16 → QF
        (89, "winner", 97, "home"),
        (90, "winner", 97, "away"),
        (91, "winner", 99, "home"),
        (92, "winner", 99, "away"),
        (93, "winner", 98, "home"),
        (94, "winner", 98, "away"),
        (95, "winner", 100, "home"),
        (96, "winner", 100, "away"),
        # QF → SF
        (97, "winner", 101, "home"),
        (98, "winner", 101, "away"),
        (99, "winner", 102, "home"),
        (100, "winner", 102, "away"),
        # SF → Final / Third place
        (101, "winner", 104, "home"),
        (102, "winner", 104, "away"),
        (101, "loser", 103, "home"),
        (102, "loser", 103, "away"),
    ]
    for src, outcome, tgt, slot in edges:
        if not BracketEdge.query.filter_by(
            source_match_number=src, source_outcome=outcome,
            target_match_number=tgt, target_slot=slot
        ).first():
            db.session.add(BracketEdge(
                source_match_number=src,
                source_outcome=outcome,
                target_match_number=tgt,
                target_slot=slot,
            ))


def _ensure_users_has_paid():
    if db.engine.url.get_backend_name() != "sqlite":
        return
    cols = [row[1] for row in db.session.execute(text("PRAGMA table_info(users)"))]
    if "has_paid" not in cols:
        db.session.execute(text("ALTER TABLE users ADD COLUMN has_paid BOOLEAN DEFAULT 0"))
        db.session.commit()


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    print(f"VM 2026 Tipping starter på http://localhost:{port}")
    print(f"Admin: http://localhost:{port}/admin")
    app.run(host="0.0.0.0", port=port, debug=debug)
