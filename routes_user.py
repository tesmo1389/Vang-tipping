"""
routes_user.py - Brukerrouter for VM 2026 tipping.
"""
import io
import secrets
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort, send_file
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from models import (
    db, User, InviteToken, Match, Group, Team, UserPrediction, GroupPrediction,
    GroupStanding, CompetitionSetting, ScoreCache, now_utc, cleanup_expired_invites
)
from scoring import (
    get_user_total_points, get_scoreboard, get_user_score_breakdown,
    recalculate_all_scores, recalculate_user_scores, get_per_match_points,
    get_prize_pool_summary, get_score_settings, SCORE_KEYS, calc_hub
)
from statistics import get_user_statistics, get_competition_statistics
import pytz

user_bp = Blueprint("user", __name__)
OSLO_TZ = pytz.timezone("Europe/Oslo")


def get_current_user():
    token = session.get("user_token")
    if not token:
        return None
    user = User.query.filter_by(token=token, is_active=True).first()
    if user:
        user.last_seen_at = now_utc()
        db.session.commit()
    return user


@user_bp.route("/join/<token>")
def join(token):
    invite = InviteToken.query.filter_by(token=token, is_active=True).first()
    if not invite:
        return render_template("error.html", message="Invitasjonslenken er ugyldig eller deaktivert."), 404

    # If already used, log in that user
    if invite.used_by_user_id:
        user = User.query.get(invite.used_by_user_id)
        if user and user.is_active:
            session["user_token"] = user.token
            return redirect(url_for("user.index"))

    return render_template("join.html", token=token)


@user_bp.route("/join/<token>", methods=["POST"])
def join_post(token):
    cleanup_expired_invites()
    
    invite = InviteToken.query.filter_by(token=token, is_active=True).first()
    if not invite:
        abort(404)

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()

    if not name:
        flash("Du må oppgi navn.", "error")
        return render_template("join.html", token=token)

    # Generate unique user token
    user_token = secrets.token_urlsafe(32)

    # Create user
    user = User(name=name, email=email, token=user_token)
    db.session.add(user)
    db.session.flush()

    # Mark invite as used
    invite.used_at = now_utc()
    invite.used_by_user_id = user.id

    db.session.commit()

    session["user_token"] = user_token
    flash(f"Velkommen, {name}! Du er nå registrert.", "success")
    return redirect(url_for("user.index"))


@user_bp.route("/")
def index():
    user = get_current_user()
    if not user:
        return render_template("no_access.html")

    group_matches_all = Match.query.filter_by(phase="group").all()
    group_total_matches = len(group_matches_all)
    group_tipped_matches = 0
    for m in group_matches_all:
        pred = UserPrediction.query.filter_by(user_id=user.id, match_id=m.id).first()
        if pred and pred.predicted_home_score is not None and pred.predicted_away_score is not None:
            group_tipped_matches += 1

    groups = Group.query.order_by(Group.name).all()
    group_data = []
    for group in groups:
        teams = Team.query.filter_by(group_id=group.id).order_by(Team.group_slot).all()
        matches = Match.query.filter_by(
            group_id=group.id, phase="group"
        ).order_by(Match.kickoff_at_utc).all()

        group_kickoffs = [m.kickoff_at_utc for m in matches if m.kickoff_at_utc]
        group_first_kickoff = min(group_kickoffs) if group_kickoffs else None
        group_lock_at_utc = group_first_kickoff - timedelta(hours=1) if group_first_kickoff else None
        group_locked = any(m.is_finished for m in matches)

        match_preds = {}
        for match in matches:
            pred = UserPrediction.query.filter_by(user_id=user.id, match_id=match.id).first()
            match_preds[match.id] = pred

        gp = GroupPrediction.query.filter_by(user_id=user.id, group_id=group.id).first()

        standings = GroupStanding.query.filter_by(group_id=group.id).all()
        if standings:
            standings_sorted = sorted(standings, key=lambda s: s.rank or 99)
        else:
            # Keep table visible even when no standings rows exist yet.
            from types import SimpleNamespace
            standings_sorted = [
                SimpleNamespace(
                    team=t,
                    played=0,
                    wins=0,
                    draws=0,
                    losses=0,
                    goals_for=0,
                    goals_against=0,
                    goal_difference=0,
                    points=0,
                    rank=i + 1,
                )
                for i, t in enumerate(teams)
            ]

        group_data.append({
            "group": group,
            "teams": teams,
            "matches": matches,
            "predictions": match_preds,
            "group_prediction": gp,
            "standings": standings_sorted,
            "group_lock_at_utc": group_lock_at_utc,
            "group_locked": group_locked,
        })

    return render_template("user.html",
        user=user,
        group_data=group_data,
        group_tipped_matches=group_tipped_matches,
        group_total_matches=group_total_matches,
        match_points=get_per_match_points(user.id),
        total_points=get_user_total_points(user.id),
        active_tab="group",
    )


@user_bp.route("/predict/match/<int:match_id>", methods=["POST"])
def predict_match(match_id):
    user = get_current_user()
    if not user:
        abort(403)

    match = Match.query.get_or_404(match_id)

    if not match.is_open_for_betting:
        flash("Denne kampen er ikke åpen for tipping.", "error")
        return redirect(url_for("user.index"))

    home_score = request.form.get("home_score", "")
    away_score = request.form.get("away_score", "")
    advanced_team_id = request.form.get("advanced_team_id")

    try:
        home_score_int = int(home_score)
        away_score_int = int(away_score)
        if home_score_int < 0 or away_score_int < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Ugyldig resultat. Oppgi gyldige tall.", "error")
        return redirect(url_for("user.index"))

    hub = request.form.get("hub", "").upper()
    if hub not in ("H", "U", "B"):
        from scoring import calc_hub
        hub = calc_hub(home_score_int, away_score_int)

    pred = UserPrediction.query.filter_by(user_id=user.id, match_id=match.id).first()
    now = now_utc()
    if pred:
        pred.predicted_home_score = home_score_int
        pred.predicted_away_score = away_score_int
        pred.predicted_hub = hub
        pred.updated_at = now
        pred.submitted_at = now
        if advanced_team_id:
            pred.predicted_advanced_team_id = int(advanced_team_id)
    else:
        pred = UserPrediction(
            user_id=user.id,
            match_id=match.id,
            predicted_home_score=home_score_int,
            predicted_away_score=away_score_int,
            predicted_hub=hub,
            predicted_advanced_team_id=int(advanced_team_id) if advanced_team_id else None,
            submitted_at=now,
        )
        db.session.add(pred)

    db.session.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True, "hub": hub, "score": f"{home_score_int}–{away_score_int}"})
    flash("Tipping lagret!", "success")
    return redirect(url_for("user.index"))


@user_bp.route("/predict/group/<int:group_id>", methods=["POST"])
def predict_group(group_id):
    user = get_current_user()
    if not user:
        abort(403)

    group = Group.query.get_or_404(group_id)

    matches = Match.query.filter_by(group_id=group.id, phase="group").all()
    group_locked = any(m.is_finished for m in matches)
    if group_locked:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            from flask import jsonify
            return jsonify({"ok": False, "error": "Gruppetipping er låst for denne gruppen."})
        flash("Gruppetipping er låst for denne gruppen.", "error")
        return redirect(url_for("user.index"))

    winner_id = request.form.get("winner_team_id")
    second_id = request.form.get("second_team_id")

    gp = GroupPrediction.query.filter_by(user_id=user.id, group_id=group_id).first()
    if gp:
        gp.predicted_winner_team_id = int(winner_id) if winner_id else None
        gp.predicted_second_team_id = int(second_id) if second_id else None
        gp.updated_at = now_utc()
    else:
        gp = GroupPrediction(
            user_id=user.id,
            group_id=group_id,
            predicted_winner_team_id=int(winner_id) if winner_id else None,
            predicted_second_team_id=int(second_id) if second_id else None,
        )
        db.session.add(gp)

    db.session.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True})
    flash(f"Gruppetipping for {group.name} lagret!", "success")
    return redirect(url_for("user.index"))


@user_bp.route("/knockout")
def knockout():
    user = get_current_user()
    if not user:
        return render_template("no_access.html")

    knockout_matches_all = Match.query.filter_by(phase="knockout").all()
    knockout_open_matches = [m for m in knockout_matches_all if m.home_team_id and m.away_team_id]
    knockout_total_matches = len(knockout_open_matches)
    knockout_tipped_matches = 0
    for m in knockout_open_matches:
        pred = UserPrediction.query.filter_by(user_id=user.id, match_id=m.id).first()
        if pred and pred.predicted_home_score is not None and pred.predicted_away_score is not None:
            knockout_tipped_matches += 1

    round_order = ["round_of_32", "round_of_16", "quarter_final", "semi_final", "third_place", "final"]
    rounds = {}
    for rn in round_order:
        matches = Match.query.filter_by(phase="knockout", round_name=rn).order_by(Match.match_number).all()
        match_data = []
        for match in matches:
            pred = UserPrediction.query.filter_by(user_id=user.id, match_id=match.id).first()
            match_data.append({"match": match, "prediction": pred})
        if matches:
            rounds[rn] = match_data

    return render_template("user.html",
        user=user,
        rounds=rounds,
        round_order=round_order,
        knockout_tipped_matches=knockout_tipped_matches,
        knockout_total_matches=knockout_total_matches,
        match_points=get_per_match_points(user.id),
        total_points=get_user_total_points(user.id),
        active_tab="knockout",
    )


@user_bp.route("/predict/knockout/<int:match_id>", methods=["POST"])
def predict_knockout(match_id):
    user = get_current_user()
    if not user:
        abort(403)

    match = Match.query.get_or_404(match_id)
    if not match.is_open_for_betting:
        flash("Denne kampen er ikke åpen for tipping.", "error")
        return redirect(url_for("user.knockout"))

    home_score = request.form.get("home_score", "")
    away_score = request.form.get("away_score", "")
    advanced_team_id = request.form.get("advanced_team_id")

    try:
        home_score_int = int(home_score)
        away_score_int = int(away_score)
        if home_score_int < 0 or away_score_int < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Ugyldig resultat.", "error")
        return redirect(url_for("user.knockout"))

    now = now_utc()
    pred = UserPrediction.query.filter_by(user_id=user.id, match_id=match.id).first()
    if pred:
        pred.predicted_home_score = home_score_int
        pred.predicted_away_score = away_score_int
        pred.predicted_advanced_team_id = int(advanced_team_id) if advanced_team_id else None
        pred.updated_at = now
        pred.submitted_at = now
    else:
        pred = UserPrediction(
            user_id=user.id,
            match_id=match.id,
            predicted_home_score=home_score_int,
            predicted_away_score=away_score_int,
            predicted_advanced_team_id=int(advanced_team_id) if advanced_team_id else None,
            submitted_at=now,
        )
        db.session.add(pred)

    db.session.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True})
    flash("Sluttspilltipping lagret!", "success")
    return redirect(url_for("user.knockout"))


@user_bp.route("/stats")
def stats():
    user = get_current_user()
    if not user:
        return render_template("no_access.html")

    recalculate_user_scores(user.id)
    user_stats = get_user_statistics(user.id)
    comp_stats = get_competition_statistics()
    scoreboard = get_scoreboard()
    total_points = get_user_total_points(user.id)
    breakdown = get_user_score_breakdown(user.id)

    # Find user rank
    user_rank = next((e["rank"] for e in scoreboard if e["user"].id == user.id), None)
    prize_summary = get_prize_pool_summary()
    prize_pool = prize_summary["prize_pool"]
    comp_name = CompetitionSetting.get("competition_name", "VM 2026 Tipping")

    return render_template("user.html",
        user=user,
        user_stats=user_stats,
        comp_stats=comp_stats,
        scoreboard=scoreboard,
        total_points=total_points,
        breakdown=breakdown,
        user_rank=user_rank,
        prize_pool=prize_pool,
        prize_summary=prize_summary,
        comp_name=comp_name,
        score_keys=SCORE_KEYS,
        active_tab="stats",
    )


@user_bp.route("/info")
def info():
    user = get_current_user()
    if not user:
        return render_template("no_access.html")

    score_settings = get_score_settings()
    return render_template("user.html",
        user=user,
        total_points=get_user_total_points(user.id),
        score_settings=score_settings,
        active_tab="info",
    )


@user_bp.route("/export/pdf")
def export_user_pdf():
    user = get_current_user()
    if not user:
        abort(403)

    match_preds = (
        db.session.query(UserPrediction, Match)
        .join(Match, UserPrediction.match_id == Match.id)
        .filter(UserPrediction.user_id == user.id)
        .order_by(Match.match_number)
        .all()
    )
    group_preds = (
        db.session.query(GroupPrediction, Group)
        .join(Group, GroupPrediction.group_id == Group.id)
        .filter(GroupPrediction.user_id == user.id)
        .order_by(Group.name)
        .all()
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Min tipping")
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Min tipping - {user.name}", styles["Title"]))
    story.append(Paragraph(f"Eksportert: {datetime.now(OSLO_TZ).strftime('%d.%m.%Y')}", styles["Normal"]))
    story.append(Spacer(1, 12))

    if group_preds:
        story.append(Paragraph("Gruppetipping", styles["Heading2"]))
        data = [["Gruppe", "Vinner", "Andreplass"]]
        for gp, group in group_preds:
            data.append([
                group.name,
                gp.predicted_winner.name if gp.predicted_winner else "-",
                gp.predicted_second.name if gp.predicted_second else "-",
            ])
        table = Table(data, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        story.append(table)
        story.append(Spacer(1, 12))

    if match_preds:
        story.append(Paragraph("Kamper (tipping og resultat)", styles["Heading2"]))
        data = [["#", "Fase", "Kamp", "Resultat", "Tipping", "HUB"]]
        for pred, match in match_preds:
            pred_hub = pred.predicted_hub
            if pred_hub is None and pred.predicted_home_score is not None and pred.predicted_away_score is not None:
                pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
            actual_hub = None
            if match.home_score is not None and match.away_score is not None:
                actual_hub = calc_hub(match.home_score, match.away_score)
            data.append([
                str(match.match_number),
                "Gruppe" if match.phase == "group" else "Sluttspill",
                f"{match.home_team.name if match.home_team else (match.home_slot_source or '-')} vs {match.away_team.name if match.away_team else (match.away_slot_source or '-')}",
                (
                    f"{match.home_score}–{match.away_score}"
                    if match.home_score is not None and match.away_score is not None else "-"
                ),
                (
                    f"{pred.predicted_home_score}–{pred.predicted_away_score}"
                    if pred.predicted_home_score is not None and pred.predicted_away_score is not None else "-"
                ),
                f"{pred_hub or '-'} / {actual_hub or '-'}",
            ])
        table = Table(data, hAlign="LEFT", colWidths=[28, 55, 200, 55, 45, 55])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        story.append(table)

    doc.build(story)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="min_tipping.pdf",
    )
