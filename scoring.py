"""
scoring.py - Poengberegning for VM 2026 tipping.
"""
import math
from datetime import datetime, timezone
from sqlalchemy import func
from models import (
    db, User, Match, UserPrediction, GroupPrediction,
    GroupStanding, ScoreCache, ScoreboardSnapshot, ScoreSetting,
    AdminAuditLog, CompetitionSetting, now_utc
)

SCORE_KEYS = [
    "group_hub",
    "group_exact",
    "group_winner",
    "group_second",
    "group_advance",
    "knockout_exact",
    "knockout_advance",
    "knockout_finalist",
    "knockout_champion",
]

DEFAULT_SCORES = {
    "group_hub": 1,
    "group_exact": 3,
    "group_winner": 5,
    "group_second": 3,
    "group_advance": 2,
    "knockout_exact": 3,
    "knockout_advance": 2,
    "knockout_finalist": 5,
    "knockout_champion": 10,
}


def get_score_settings():
    settings = {}
    for k, v in DEFAULT_SCORES.items():
        settings[k] = ScoreSetting.get(k, v)
    return settings


def get_prize_pool_summary():
    """Calculate prize pool from entry fee and active participants.

    Rules:
    - Entry fee per participant
    - 9% admin fee
    - Remaining amount rounded up to nearest 25 NOK
    """
    raw_entry_fee = CompetitionSetting.get("entry_fee", "250")
    try:
        entry_fee = int(float(raw_entry_fee))
    except (ValueError, TypeError):
        entry_fee = 250
    entry_fee = max(0, entry_fee)

    participants = User.query.filter_by(is_active=True).count()
    gross_amount = participants * entry_fee
    admin_fee_percent = 9
    net_amount = gross_amount * (100 - admin_fee_percent) / 100.0

    if net_amount <= 0:
        prize_pool = 0
    else:
        prize_pool = int(math.ceil(net_amount / 25.0) * 25)

    return {
        "entry_fee": entry_fee,
        "participants": participants,
        "gross_amount": gross_amount,
        "admin_fee_percent": admin_fee_percent,
        "net_amount": int(round(net_amount)),
        "prize_pool": prize_pool,
    }


def calc_hub(home, away):
    if home > away:
        return "H"
    elif home == away:
        return "U"
    else:
        return "B"


def calculate_user_scores(user_id):
    """Calculate scores for a single user across all categories. Returns dict."""
    settings = get_score_settings()
    scores = {k: 0 for k in SCORE_KEYS}

    # Group stage match predictions
    group_matches = Match.query.filter_by(phase="group", is_finished=True).all()
    for match in group_matches:
        if match.home_score is None or match.away_score is None:
            continue
        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred:
            continue
        if pred.predicted_home_score is None or pred.predicted_away_score is None:
            continue

        actual_hub = calc_hub(match.home_score, match.away_score)
        pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)

        if pred_hub == actual_hub:
            scores["group_hub"] += settings["group_hub"]
            if (pred.predicted_home_score == match.home_score and
                    pred.predicted_away_score == match.away_score):
                scores["group_exact"] += settings["group_exact"] - settings["group_hub"]

    # Group position predictions
    groups_with_standings = db.session.query(GroupStanding.group_id).distinct().all()
    for (group_id,) in groups_with_standings:
        gp = GroupPrediction.query.filter_by(user_id=user_id, group_id=group_id).first()
        if not gp:
            continue

        standings = GroupStanding.query.filter_by(group_id=group_id).all()
        ranked = sorted(standings, key=lambda s: s.rank or 99)
        actual_ids = [s.team_id for s in ranked]

        if len(actual_ids) >= 1 and gp.predicted_winner_team_id:
            if gp.predicted_winner_team_id == actual_ids[0]:
                scores["group_winner"] += settings["group_winner"]
        if len(actual_ids) >= 2 and gp.predicted_second_team_id:
            if gp.predicted_second_team_id == actual_ids[1]:
                scores["group_second"] += settings["group_second"]

        # Correct team advancing (1st or 2nd)
        advancing = set(actual_ids[:2])
        predicted_advancing = set()
        if gp.predicted_winner_team_id:
            predicted_advancing.add(gp.predicted_winner_team_id)
        if gp.predicted_second_team_id:
            predicted_advancing.add(gp.predicted_second_team_id)
        for team_id in predicted_advancing:
            if team_id in advancing:
                scores["group_advance"] += settings["group_advance"]

    # Knockout predictions
    knockout_matches = Match.query.filter_by(phase="knockout", is_finished=True).all()
    for match in knockout_matches:
        if match.home_score is None or match.away_score is None:
            continue
        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred or not pred.is_valid:
            continue
        if pred.predicted_home_score is None or pred.predicted_away_score is None:
            continue

        if (pred.predicted_home_score == match.home_score and
                pred.predicted_away_score == match.away_score):
            scores["knockout_exact"] += settings["knockout_exact"]

        if (pred.predicted_advanced_team_id and match.advanced_team_id and
                pred.predicted_advanced_team_id == match.advanced_team_id):
            adv_pts = settings["knockout_advance"]

            # Bonus for finalist and champion
            if match.round_name == "final":
                adv_pts = settings["knockout_champion"]
            elif match.round_name == "semi_final":
                adv_pts = settings["knockout_finalist"]

            scores["knockout_advance"] += adv_pts

    return scores


def _save_user_score_cache(user_id, scores):
    """Write a scores dict to ScoreCache for one user."""
    for category, points in scores.items():
        cache = ScoreCache.query.filter_by(user_id=user_id, category=category).first()
        if cache:
            cache.points = points
            cache.recalculated_at = now_utc()
        else:
            db.session.add(ScoreCache(
                user_id=user_id,
                category=category,
                points=points,
                recalculated_at=now_utc()
            ))


def recalculate_user_scores(user_id):
    """Recalculate and cache scores for a single user (fast path)."""
    scores = calculate_user_scores(user_id)
    _save_user_score_cache(user_id, scores)
    db.session.commit()


def recalculate_all_scores():
    """Recalculate scores for all active users and update score_cache."""
    users = User.query.filter_by(is_active=True).all()
    for user in users:
        scores = calculate_user_scores(user.id)
        _save_user_score_cache(user.id, scores)

    db.session.commit()

    # Update scoreboard snapshots
    update_scoreboard_snapshots()

    db.session.add(AdminAuditLog(action="recalculate_scores", details="All scores recalculated"))
    db.session.commit()


def get_user_total_points(user_id):
    result = db.session.query(func.sum(ScoreCache.points)).filter_by(user_id=user_id).scalar()
    return result or 0


def get_scoreboard():
    """Return sorted list of (user, total_points, rank)."""
    users = User.query.filter_by(is_active=True).all()
    board = []
    for user in users:
        pts = get_user_total_points(user.id)
        board.append((user, pts))
    board.sort(key=lambda x: -x[1])
    result = []
    for i, (user, pts) in enumerate(board):
        result.append({"user": user, "points": pts, "rank": i + 1})
    return result


def update_scoreboard_snapshots():
    """Save current scoreboard as snapshot."""
    board = get_scoreboard()
    ts = now_utc()
    for entry in board:
        snap = ScoreboardSnapshot(
            user_id=entry["user"].id,
            rank=entry["rank"],
            points=entry["points"],
            created_at=ts
        )
        db.session.add(snap)
    db.session.commit()


def get_user_score_breakdown(user_id):
    """Return per-category score breakdown for a user."""
    caches = ScoreCache.query.filter_by(user_id=user_id).all()
    breakdown = {k: 0 for k in SCORE_KEYS}
    for c in caches:
        if c.category in breakdown:
            breakdown[c.category] = c.points
    return breakdown


def get_per_match_points(user_id):
    """Return dict of match_id -> points earned by user for that match."""
    settings = get_score_settings()
    result = {}

    group_matches = Match.query.filter_by(phase="group", is_finished=True).all()
    for match in group_matches:
        if match.home_score is None or match.away_score is None:
            result[match.id] = None
            continue
        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred or pred.predicted_home_score is None:
            result[match.id] = None
            continue
        actual_hub = calc_hub(match.home_score, match.away_score)
        pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
        if pred_hub == actual_hub:
            if (pred.predicted_home_score == match.home_score and
                    pred.predicted_away_score == match.away_score):
                result[match.id] = settings["group_exact"]
            else:
                result[match.id] = settings["group_hub"]
        else:
            result[match.id] = 0

    ko_matches = Match.query.filter_by(phase="knockout", is_finished=True).all()
    for match in ko_matches:
        if match.home_score is None or match.away_score is None:
            result[match.id] = None
            continue
        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred or not pred.is_valid or pred.predicted_home_score is None:
            result[match.id] = None
            continue
        pts = 0
        if (pred.predicted_home_score == match.home_score and
                pred.predicted_away_score == match.away_score):
            pts += settings["knockout_exact"]
        if (pred.predicted_advanced_team_id and match.advanced_team_id and
                pred.predicted_advanced_team_id == match.advanced_team_id):
            if match.round_name == "final":
                pts += settings["knockout_champion"]
            elif match.round_name == "semi_final":
                pts += settings["knockout_finalist"]
            else:
                pts += settings["knockout_advance"]
        result[match.id] = pts

    return result
