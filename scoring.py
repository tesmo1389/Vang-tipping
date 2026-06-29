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

    group_matches_all = Match.query.filter_by(phase="group").all()
    group_complete = {}
    for gm in group_matches_all:
        if gm.group_id is None:
            continue
        group_complete[gm.group_id] = group_complete.get(gm.group_id, True) and bool(gm.is_finished)

    # Group stage match predictions
    group_matches = Match.query.filter_by(phase="group", is_finished=True).all()
    for match in group_matches:
        if match.home_score is None or match.away_score is None:
            continue
        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred:
            continue

        actual_hub = calc_hub(match.home_score, match.away_score)
        if pred.predicted_home_score is not None and pred.predicted_away_score is not None:
            pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
        else:
            pred_hub = pred.predicted_hub

        if pred_hub == actual_hub:
            scores["group_hub"] += settings["group_hub"]
            if (pred.predicted_home_score is not None and pred.predicted_away_score is not None and
                    pred.predicted_home_score == match.home_score and
                    pred.predicted_away_score == match.away_score):
                scores["group_exact"] += settings["group_exact"]

    # Group position predictions
    groups_with_standings = db.session.query(GroupStanding.group_id).distinct().all()
    for (group_id,) in groups_with_standings:
        # Check if ALL matches in this specific group are finished
        group_matches_count = Match.query.filter_by(group_id=group_id, phase="group").count()
        group_finished_count = Match.query.filter_by(group_id=group_id, phase="group", is_finished=True).count()
        
        # Only award points if all matches in this group are complete
        if group_matches_count == 0 or group_finished_count != group_matches_count:
            continue

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


def _get_user_tiebreak_counts(user_id):
    """Return tie-break counts: exact score hits and correct HUB hits."""
    exact_hits = 0
    hub_hits = 0

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
        predicted_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
        if predicted_hub == actual_hub:
            hub_hits += 1

        if (pred.predicted_home_score == match.home_score and
                pred.predicted_away_score == match.away_score):
            exact_hits += 1

    knockout_matches = Match.query.filter_by(phase="knockout", is_finished=True).all()
    for match in knockout_matches:
        if match.home_score is None or match.away_score is None:
            continue

        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred or not pred.is_valid:
            continue
        if pred.predicted_home_score is None or pred.predicted_away_score is None:
            continue

        actual_hub = calc_hub(match.home_score, match.away_score)
        predicted_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
        if predicted_hub == actual_hub:
            hub_hits += 1

        if (pred.predicted_home_score == match.home_score and
                pred.predicted_away_score == match.away_score):
            exact_hits += 1

    return exact_hits, hub_hits


def get_scoreboard():
    """Return sorted list of (user, total_points, rank)."""
    users = User.query.filter_by(is_active=True).all()
    board = []
    for user in users:
        pts = get_user_total_points(user.id)
        exact_hits, hub_hits = _get_user_tiebreak_counts(user.id)
        breakdown = get_user_score_breakdown(user.id)
        board.append((user, pts, exact_hits, hub_hits, breakdown))
    board.sort(key=lambda x: (-x[1], -x[2], -x[3], x[0].id))
    result = []
    for i, (user, pts, exact_hits, hub_hits, breakdown) in enumerate(board):
        exact_points = breakdown.get("group_exact", 0) + breakdown.get("knockout_exact", 0)
        hub_points = breakdown.get("group_hub", 0)
        result.append({
            "user": user,
            "points": pts,
            "exact_hits": exact_hits,
            "hub_hits": hub_hits,
            "exact_points": exact_points,
            "hub_points": hub_points,
            "rank": i + 1,
            "breakdown": breakdown,
        })
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
        if not pred:
            result[match.id] = None
            continue
        actual_hub = calc_hub(match.home_score, match.away_score)
        if pred.predicted_home_score is not None and pred.predicted_away_score is not None:
            pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
        else:
            pred_hub = pred.predicted_hub
        if pred_hub == actual_hub:
            if (pred.predicted_home_score is not None and pred.predicted_away_score is not None and
                    pred.predicted_home_score == match.home_score and
                    pred.predicted_away_score == match.away_score):
                result[match.id] = settings["group_exact"] + settings["group_hub"]
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


def get_statistics():
    """Samle statistikk for admin statistikk-side."""
    scoreboard = get_scoreboard()
    settings = get_score_settings()
    
    # Grunnleggende statistikk
    num_users = User.query.count()
    total_points = sum(entry['points'] for entry in scoreboard)
    avg_points = total_points / num_users if num_users > 0 else 0
    avg_exact_hits = sum(entry['exact_hits'] for entry in scoreboard) / num_users if num_users > 0 else 0
    avg_hub_hits = sum(entry['hub_hits'] for entry in scoreboard) / num_users if num_users > 0 else 0
    
    # Poengfordeling per kategori
    score_distribution = {key: 0 for key in SCORE_KEYS}
    score_counts = {key: 0 for key in SCORE_KEYS}  # Antall ganger poeng ble gitt
    
    for user in User.query.all():
        breakdown = get_user_score_breakdown(user.id)
        for key in SCORE_KEYS:
            if key in breakdown:
                points = breakdown[key]
                score_distribution[key] += points
                if points > 0:
                    score_counts[key] += 1
    
    # Top 10 brukere
    top_users = scoreboard[:10] if len(scoreboard) > 0 else []
    
    # Bottom 10 brukere
    bottom_users = scoreboard[-10:] if len(scoreboard) > 0 else []
    bottom_users.reverse()  # Omvendt rekkefølge
    
    # Poengfordeling histogram
    point_ranges = {
        "0-20": 0,
        "20-50": 0,
        "50-100": 0,
        "100-150": 0,
        "150+": 0,
    }
    for entry in scoreboard:
        pts = entry['points']
        if pts < 20:
            point_ranges["0-20"] += 1
        elif pts < 50:
            point_ranges["20-50"] += 1
        elif pts < 100:
            point_ranges["50-100"] += 1
        elif pts < 150:
            point_ranges["100-150"] += 1
        else:
            point_ranges["150+"] += 1
    
    # Gruppe vs Knockout statistikk
    group_preds_exact = UserPrediction.query.join(Match).filter(
        Match.phase == "group",
        UserPrediction.predicted_home_score == Match.home_score,
        UserPrediction.predicted_away_score == Match.away_score
    ).count()
    group_preds_total = UserPrediction.query.join(Match).filter(
        Match.phase == "group",
        Match.is_finished == True
    ).count()
    group_accuracy = round((group_preds_exact / group_preds_total * 100) if group_preds_total > 0 else 0, 1)
    
    ko_preds_exact = UserPrediction.query.join(Match).filter(
        Match.phase == "knockout",
        UserPrediction.predicted_home_score == Match.home_score,
        UserPrediction.predicted_away_score == Match.away_score
    ).count()
    ko_preds_total = UserPrediction.query.join(Match).filter(
        Match.phase == "knockout",
        Match.is_finished == True
    ).count()
    ko_accuracy = round((ko_preds_exact / ko_preds_total * 100) if ko_preds_total > 0 else 0, 1)
    
    # Gjennomsnittlig poeng per kategori
    avg_score_per_category = {}
    for key in SCORE_KEYS:
        if score_counts[key] > 0:
            avg_score_per_category[key] = round(score_distribution[key] / score_counts[key], 2)
        else:
            avg_score_per_category[key] = 0
    
    # Statistikk om prediksjonsfordeling (eksakt, HUB, ingenting)
    exact_count = sum(entry['exact_hits'] for entry in scoreboard)
    hub_only_count = sum(entry['hub_hits'] - entry['exact_hits'] for entry in scoreboard)
    no_match_count = group_preds_total - exact_count - hub_only_count if group_preds_total > 0 else 0
    
    return {
        "num_users": num_users,
        "total_points": total_points,
        "avg_points": round(avg_points, 2),
        "avg_exact_hits": round(avg_exact_hits, 2),
        "avg_hub_hits": round(avg_hub_hits, 2),
        "score_distribution": score_distribution,
        "score_counts": score_counts,
        "avg_score_per_category": avg_score_per_category,
        "top_users": top_users,
        "bottom_users": bottom_users,
        "point_ranges": point_ranges,
        "group_accuracy": group_accuracy,
        "ko_accuracy": ko_accuracy,
        "exact_count": exact_count,
        "hub_only_count": max(0, hub_only_count),
        "no_match_count": max(0, no_match_count),
        "score_settings": settings,
    }
