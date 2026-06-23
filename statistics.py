"""
statistics.py - Bruker- og konkurransestatistikk for VM 2026 tipping.
"""
from sqlalchemy import func
from models import (
    db, User, Match, UserPrediction, GroupPrediction,
    ScoreCache, GroupStanding
)
from scoring import (
    get_user_total_points, get_scoreboard,
    get_user_score_breakdown, calc_hub, SCORE_KEYS
)


def get_user_statistics(user_id):
    """Returnerer statistikk for en bruker."""
    group_matches = Match.query.filter_by(phase="group", is_finished=True).all()
    knockout_matches = Match.query.filter_by(phase="knockout", is_finished=True).all()

    total_group = len(group_matches)
    total_knockout = len(knockout_matches)

    correct_hub = 0
    correct_exact_group = 0
    correct_exact_knockout = 0
    correct_advance = 0
    perfect_total = 0

    for match in group_matches:
        if match.home_score is None:
            continue
        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred:
            continue
        actual_hub = calc_hub(match.home_score, match.away_score)
        pred_hub = pred.predicted_hub
        if pred_hub not in ("H", "U", "B") and pred.predicted_home_score is not None and pred.predicted_away_score is not None:
            pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
        if pred_hub == actual_hub:
            correct_hub += 1
        if (pred.predicted_home_score == match.home_score and
                pred.predicted_away_score == match.away_score):
            correct_exact_group += 1
            perfect_total += 1

    for match in knockout_matches:
        if match.home_score is None:
            continue
        pred = UserPrediction.query.filter_by(user_id=user_id, match_id=match.id).first()
        if not pred or not pred.is_valid:
            continue
        if pred.predicted_home_score is None:
            continue
        if (pred.predicted_home_score == match.home_score and
                pred.predicted_away_score == match.away_score):
            correct_exact_knockout += 1
            perfect_total += 1
        if pred.predicted_advanced_team_id and pred.predicted_advanced_team_id == match.advanced_team_id:
            correct_advance += 1

    total_finished = total_group + total_knockout
    total_points = get_user_total_points(user_id)

    # Correct group winners
    correct_group_winners = 0
    groups_with_standings = db.session.query(GroupStanding.group_id).distinct().all()
    for (group_id,) in groups_with_standings:
        gp = GroupPrediction.query.filter_by(user_id=user_id, group_id=group_id).first()
        if not gp or not gp.predicted_winner_team_id:
            continue
        standings = GroupStanding.query.filter_by(group_id=group_id).all()
        ranked = sorted(standings, key=lambda s: s.rank or 99)
        if ranked and ranked[0].team_id == gp.predicted_winner_team_id:
            correct_group_winners += 1

    breakdown = get_user_score_breakdown(user_id)

    return {
        "total_points": total_points,
        "total_finished": total_finished,
        "correct_hub": correct_hub,
        "correct_exact_group": correct_exact_group,
        "correct_exact_knockout": correct_exact_knockout,
        "correct_advance": correct_advance,
        "perfect_total": perfect_total,
        "correct_group_winners": correct_group_winners,
        "hit_rate": round(100 * (correct_hub + correct_exact_group + correct_exact_knockout) / max(total_finished, 1), 1),
        "avg_points_per_match": round(total_points / max(total_finished, 1), 2),
        "breakdown": breakdown,
    }


def get_competition_statistics():
    """Returnerer generell konkurransestatistikk."""
    users = User.query.filter_by(is_active=True).all()
    if not users:
        return {}

    all_points = []
    for user in users:
        pts = get_user_total_points(user.id)
        all_points.append(pts)

    all_points.sort()
    n = len(all_points)
    median = all_points[n // 2] if n else 0
    avg = round(sum(all_points) / max(n, 1), 1)

    total_finished = Match.query.filter_by(is_finished=True).count()
    total_unfinished = Match.query.filter(
        Match.kickoff_at_utc.isnot(None),
        Match.is_finished == False
    ).count()

    users_with_group_preds = db.session.query(
        func.count(func.distinct(GroupPrediction.user_id))
    ).scalar() or 0

    users_with_ko_preds = db.session.query(
        func.count(func.distinct(UserPrediction.user_id))
    ).join(Match).filter(Match.phase == "knockout").scalar() or 0

    return {
        "num_participants": n,
        "avg_points": avg,
        "max_points": max(all_points) if all_points else 0,
        "min_points": min(all_points) if all_points else 0,
        "median_points": median,
        "total_finished": total_finished,
        "total_unfinished": total_unfinished,
        "users_with_group_preds": users_with_group_preds,
        "users_with_ko_preds": users_with_ko_preds,
    }
