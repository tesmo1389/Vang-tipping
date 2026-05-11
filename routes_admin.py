"""
routes_admin.py - Adminrouter for VM 2026 tipping.
"""
import io
import secrets
import qrcode
import base64
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, send_file, abort, jsonify
)
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from models import (
    db, User, InviteToken, Match, Group, Team, GroupStanding,
    ThirdPlaceRanking, CompetitionSetting, ScoreSetting,
    AdminAuditLog, ScoreCache, ScoreboardSnapshot,
    UserPrediction, GroupPrediction, now_utc
)
from scoring import (
    recalculate_all_scores, get_scoreboard, get_prize_pool_summary, DEFAULT_SCORES,
    get_user_total_points, get_user_score_breakdown, get_per_match_points,
    get_score_settings, calc_hub
)
from bracket import (
    advance_team_in_bracket, fill_round_of_32,
    calculate_group_standings, calculate_third_place_rankings
)
from import_export import (
    generate_excel_template, generate_csv_template,
    validate_and_preview_import, execute_import, export_users_csv
)
from schedule_import import import_schedule_from_csv
import pytz

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
OSLO_TZ = pytz.timezone("Europe/Oslo")


def admin_required():
    return session.get("admin_logged_in") is True


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        import os
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        admin_user = os.environ.get("ADMIN_USERNAME", "admin")
        admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
        if username == admin_user and password == admin_pass:
            session["admin_logged_in"] = True
            db.session.add(AdminAuditLog(action="admin_login", details=f"Login: {username}"))
            db.session.commit()
            return redirect(url_for("admin.dashboard"))
        flash("Feil brukernavn eller passord.", "error")
    return render_template("admin_login.html")


@admin_bp.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin.login"))


@admin_bp.route("/")
def dashboard():
    if not admin_required():
        return redirect(url_for("admin.login"))
    return redirect(url_for("admin.tab", tab="groups"))


# ── Groups & Group Matches ─────────────────────────────────────────────────────

@admin_bp.route("/match/<int:match_id>/result", methods=["POST"])
def save_match_result(match_id):
    if not admin_required():
        abort(403)

    match = Match.query.get_or_404(match_id)
    home_score = request.form.get("home_score", "")
    away_score = request.form.get("away_score", "")
    is_finished = request.form.get("is_finished") == "1"
    result_note = request.form.get("result_note", "").strip()
    advanced_team_id = request.form.get("advanced_team_id")

    try:
        if home_score != "":
            match.home_score = int(home_score)
        if away_score != "":
            match.away_score = int(away_score)
    except (ValueError, TypeError):
        flash("Ugyldig poengsum.", "error")
        return redirect(url_for("admin.dashboard"))

    match.is_finished = is_finished
    match.result_note = result_note

    # For knockout: auto-determine winner from score if clear (not a draw)
    if match.phase == "knockout" and match.home_score is not None and match.away_score is not None:
        if match.home_score > match.away_score:
            advanced_team_id = str(match.home_team_id)
        elif match.away_score > match.home_score:
            advanced_team_id = str(match.away_team_id)
        # If equal: keep the manually submitted advanced_team_id (penalties)

    if advanced_team_id:
        match.advanced_team_id = int(advanced_team_id)
        # Calculate loser
        if match.home_team_id and match.away_team_id:
            if int(advanced_team_id) == match.home_team_id:
                match.loser_team_id = match.away_team_id
            else:
                match.loser_team_id = match.home_team_id

    db.session.commit()

    if is_finished and match.phase == "group":
        calculate_group_standings()

    if is_finished and match.phase == "knockout" and match.advanced_team_id:
        advance_team_in_bracket(match.match_number, match.advanced_team_id, match.loser_team_id)

    if is_finished:
        recalculate_all_scores()

    db.session.add(AdminAuditLog(
        action="save_result",
        details=f"Kamp {match.match_number}: {match.home_score}-{match.away_score}, ferdig={is_finished}"
    ))
    db.session.commit()
    msg = f"Resultat for kamp {match.match_number} lagret."
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True, "flash": msg})
    flash(msg, "success")
    next_tab = request.form.get("next_tab", "groups")
    return redirect(url_for("admin.tab", tab=next_tab))


@admin_bp.route("/tab/<tab>")
def tab(tab):
    if not admin_required():
        return redirect(url_for("admin.login"))

    groups = Group.query.order_by(Group.name).all()
    teams = Team.query.order_by(Team.name).all()
    group_matches = Match.query.filter_by(phase="group").order_by(Match.match_number).all()
    knockout_matches = Match.query.filter_by(phase="knockout").order_by(Match.match_number).all()
    all_matches = Match.query.order_by(Match.match_number).all()
    users = User.query.order_by(User.created_at.desc()).all()
    invite_tokens = InviteToken.query.order_by(InviteToken.created_at.desc()).all()

    standings_by_group = {}
    for group in groups:
        st = GroupStanding.query.filter_by(group_id=group.id).all()
        if st:
            standings_by_group[group.id] = sorted(st, key=lambda s: s.rank or 99)
        else:
            # Keep table visible even before standings rows are persisted.
            from types import SimpleNamespace
            team_rows = Team.query.filter_by(group_id=group.id).order_by(Team.group_slot).all()
            standings_by_group[group.id] = [
                SimpleNamespace(
                    team=t,
                    team_id=t.id,
                    played=0,
                    wins=0,
                    draws=0,
                    losses=0,
                    goals_for=0,
                    goals_against=0,
                    goal_difference=0,
                    points=0,
                    rank=i + 1,
                    manual_rank_override=None,
                )
                for i, t in enumerate(team_rows)
            ]

    comp_settings = {s.key: s.value for s in CompetitionSetting.query.all()}
    score_settings = {s.key: s.value for s in ScoreSetting.query.all()}
    prize_summary = get_prize_pool_summary()

    scoreboard = get_scoreboard()

    return render_template("admin.html",
        active_tab=tab,
        groups=groups,
        teams=teams,
        group_matches=group_matches,
        knockout_matches=knockout_matches,
        all_matches=all_matches,
        users=users,
        invite_tokens=invite_tokens,
        standings_by_group=standings_by_group,
        comp_settings=comp_settings,
        prize_summary=prize_summary,
        score_settings=score_settings,
        default_scores=DEFAULT_SCORES,
        scoreboard=scoreboard,
    )


@admin_bp.route("/user/<int:user_id>")
def user_detail(user_id):
    if not admin_required():
        abort(403)

    user = User.query.get_or_404(user_id)
    sub_tab = request.args.get("tab", "points")
    settings = get_score_settings()
    per_match_points = get_per_match_points(user.id)

    match_rows_group = []
    match_rows_knockout = []
    match_preds = (
        db.session.query(UserPrediction, Match)
        .join(Match, UserPrediction.match_id == Match.id)
        .filter(UserPrediction.user_id == user.id)
        .order_by(Match.match_number)
        .all()
    )

    for pred, match in match_preds:
        pred_hub = pred.predicted_hub
        if pred_hub is None and pred.predicted_home_score is not None and pred.predicted_away_score is not None:
            pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)

        actual_hub = None
        if match.home_score is not None and match.away_score is not None:
            actual_hub = calc_hub(match.home_score, match.away_score)

        row = {
            "match_number": match.match_number,
            "phase": match.phase,
            "round_name": match.round_name,
            "group_name": match.group.name if match.group else None,
            "home": match.home_team.name if match.home_team else (match.home_slot_source or "-")
        }
        row.update({
            "away": match.away_team.name if match.away_team else (match.away_slot_source or "-"),
            "pred_score": (
                f"{pred.predicted_home_score}–{pred.predicted_away_score}"
                if pred.predicted_home_score is not None and pred.predicted_away_score is not None else "-"
            ),
            "pred_hub": pred_hub or "-",
            "pred_advanced": pred.predicted_advanced_team.name if pred.predicted_advanced_team else "-",
            "actual_score": (
                f"{match.home_score}–{match.away_score}"
                if match.home_score is not None and match.away_score is not None else "-"
            ),
            "actual_hub": actual_hub or "-",
            "actual_advanced": match.advanced_team.name if match.advanced_team else "-",
            "points": per_match_points.get(match.id),
            "is_finished": match.is_finished,
        })
        if match.phase == "group":
            match_rows_group.append(row)
        else:
            match_rows_knockout.append(row)

    group_rows = []
    group_preds = (
        db.session.query(GroupPrediction, Group)
        .join(Group, GroupPrediction.group_id == Group.id)
        .filter(GroupPrediction.user_id == user.id)
        .order_by(Group.name)
        .all()
    )
    for gp, group in group_preds:
        standings = GroupStanding.query.filter_by(group_id=group.id).all()
        ranked = sorted(standings, key=lambda s: s.rank or 99)
        actual_ids = [s.team_id for s in ranked]
        actual_winner = ranked[0].team.name if len(ranked) >= 1 and ranked[0].team else "-"
        actual_second = ranked[1].team.name if len(ranked) >= 2 and ranked[1].team else "-"

        winner_pts = settings["group_winner"] if gp.predicted_winner_team_id and actual_ids and gp.predicted_winner_team_id == actual_ids[0] else 0
        second_pts = settings["group_second"] if gp.predicted_second_team_id and len(actual_ids) >= 2 and gp.predicted_second_team_id == actual_ids[1] else 0

        advance_pts = 0
        predicted_advancing = set()
        if gp.predicted_winner_team_id:
            predicted_advancing.add(gp.predicted_winner_team_id)
        if gp.predicted_second_team_id:
            predicted_advancing.add(gp.predicted_second_team_id)
        for team_id in predicted_advancing:
            if team_id in actual_ids[:2]:
                advance_pts += settings["group_advance"]

        group_rows.append({
            "group_name": group.name,
            "pred_winner": gp.predicted_winner.name if gp.predicted_winner else "-",
            "pred_second": gp.predicted_second.name if gp.predicted_second else "-",
            "actual_winner": actual_winner,
            "actual_second": actual_second,
            "winner_pts": winner_pts,
            "second_pts": second_pts,
            "advance_pts": advance_pts,
            "total_pts": winner_pts + second_pts + advance_pts,
        })

    return render_template("admin_user.html",
        user=user,
        active_tab="users",
        active_subtab=sub_tab,
        total_points=get_user_total_points(user.id),
        breakdown=get_user_score_breakdown(user.id),
        group_rows=group_rows,
        match_rows_group=match_rows_group,
        match_rows_knockout=match_rows_knockout,
    )


@admin_bp.route("/user/<int:user_id>/predictions.pdf")
def user_predictions_pdf(user_id):
    if not admin_required():
        abort(403)

    user = User.query.get_or_404(user_id)
    settings = get_score_settings()
    per_match_points = get_per_match_points(user.id)
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

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    except Exception:
        flash("PDF-bibliotek mangler. Installer reportlab for PDF-eksport.", "error")
        return redirect(url_for("admin.user_detail", user_id=user.id, tab="tips"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Tipping - tips")
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Tipping - tips for {user.name}", styles["Title"]))
    story.append(Paragraph(f"Eksportert: {now_utc().strftime('%d.%m.%Y %H:%M')}", styles["Normal"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Poengoversikt", styles["Heading2"]))
    breakdown = get_user_score_breakdown(user.id)
    score_data = [["Kategori", "Poeng"]]
    score_data.append(["Totalt", str(get_user_total_points(user.id))])
    for k, v in breakdown.items():
        score_data.append([k, str(v)])
    table = Table(score_data, hAlign="LEFT", colWidths=[180, 60])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
    ]))
    story.append(table)
    story.append(Spacer(1, 12))

    if group_preds:
        story.append(Paragraph("Gruppeplasseringer (poeng)", styles["Heading2"]))
        data = [["Gruppe", "Vinner (tips)", "Vinner (fasit)", "Andre (tips)", "Andre (fasit)", "Vinner", "Andre", "Videre", "Sum"]]
        for gp, group in group_preds:
            standings = GroupStanding.query.filter_by(group_id=group.id).all()
            ranked = sorted(standings, key=lambda s: s.rank or 99)
            actual_ids = [s.team_id for s in ranked]
            actual_winner = ranked[0].team.name if len(ranked) >= 1 and ranked[0].team else "-"
            actual_second = ranked[1].team.name if len(ranked) >= 2 and ranked[1].team else "-"

            winner_pts = settings["group_winner"] if gp.predicted_winner_team_id and actual_ids and gp.predicted_winner_team_id == actual_ids[0] else 0
            second_pts = settings["group_second"] if gp.predicted_second_team_id and len(actual_ids) >= 2 and gp.predicted_second_team_id == actual_ids[1] else 0

            advance_pts = 0
            predicted_advancing = set()
            if gp.predicted_winner_team_id:
                predicted_advancing.add(gp.predicted_winner_team_id)
            if gp.predicted_second_team_id:
                predicted_advancing.add(gp.predicted_second_team_id)
            for team_id in predicted_advancing:
                if team_id in actual_ids[:2]:
                    advance_pts += settings["group_advance"]

            data.append([
                group.name,
                gp.predicted_winner.name if gp.predicted_winner else "-",
                actual_winner,
                gp.predicted_second.name if gp.predicted_second else "-",
                actual_second,
                str(winner_pts),
                str(second_pts),
                str(advance_pts),
                str(winner_pts + second_pts + advance_pts),
            ])
        table = Table(data, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        story.append(table)
        story.append(Spacer(1, 12))

    if match_preds:
        group_rows = []
        knockout_rows = []
        for pred, match in match_preds:
            pred_hub = pred.predicted_hub
            if pred_hub is None and pred.predicted_home_score is not None and pred.predicted_away_score is not None:
                pred_hub = calc_hub(pred.predicted_home_score, pred.predicted_away_score)
            actual_hub = None
            if match.home_score is not None and match.away_score is not None:
                actual_hub = calc_hub(match.home_score, match.away_score)
            row = [
                str(match.match_number),
                f"{match.home_team.name if match.home_team else (match.home_slot_source or '-')} vs {match.away_team.name if match.away_team else (match.away_slot_source or '-')}" ,
                (
                    f"{match.home_score}–{match.away_score}"
                    if match.home_score is not None and match.away_score is not None else "-"
                ),
                (
                    f"{pred.predicted_home_score}–{pred.predicted_away_score}"
                    if pred.predicted_home_score is not None and pred.predicted_away_score is not None else "-"
                ),
                f"{pred_hub or '-'} / {actual_hub or '-'}",
                pred.predicted_advanced_team.name if pred.predicted_advanced_team else "-",
                str(per_match_points.get(match.id)) if per_match_points.get(match.id) is not None else "-",
            ]
            if match.phase == "group":
                group_rows.append(row)
            else:
                knockout_rows.append(row)

        table_header = [["#", "Kamp", "Fasit", "Tips", "HUB", "Videre", "Poeng"]]
        table_style = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ])

        if group_rows:
            story.append(Paragraph("Gruppespill (poeng og tips)", styles["Heading2"]))
            table = Table(table_header + group_rows, hAlign="LEFT", colWidths=[28, 220, 45, 45, 60, 65, 40])
            table.setStyle(table_style)
            story.append(table)
            story.append(Spacer(1, 12))

        if knockout_rows:
            story.append(Paragraph("Sluttspill (poeng og tips)", styles["Heading2"]))
            table = Table(table_header + knockout_rows, hAlign="LEFT", colWidths=[28, 220, 45, 45, 60, 65, 40])
            table.setStyle(table_style)
            story.append(table)

    doc.build(story)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"tips_user_{user.id}.pdf"
    )


@admin_bp.route("/group/add", methods=["POST"])
def add_group():
    if not admin_required():
        abort(403)
    name = request.form.get("name", "").strip()
    color = request.form.get("color", "#4a90e2").strip()
    if name:
        if not Group.query.filter_by(name=name).first():
            db.session.add(Group(name=name, color=color))
            db.session.commit()
            flash(f"Gruppe '{name}' opprettet.", "success")
    return redirect(url_for("admin.tab", tab="groups"))


@admin_bp.route("/team/add", methods=["POST"])
def add_team():
    if not admin_required():
        abort(403)
    name = request.form.get("name", "").strip()
    fifa_code = request.form.get("fifa_code", "").strip().upper()
    group_id = request.form.get("group_id")
    if name:
        team = Team(
            name=name,
            fifa_code=fifa_code or None,
            group_id=int(group_id) if group_id else None
        )
        db.session.add(team)
        db.session.commit()
        flash(f"Lag '{name}' opprettet.", "success")
    return redirect(url_for("admin.tab", tab="groups"))


@admin_bp.route("/match/add", methods=["POST"])
def add_match():
    if not admin_required():
        abort(403)
    match_number = request.form.get("match_number")
    phase = request.form.get("phase", "group")
    round_name = request.form.get("round_name", "group_stage")
    group_id = request.form.get("group_id")
    home_team_id = request.form.get("home_team_id")
    away_team_id = request.form.get("away_team_id")
    kickoff_str = request.form.get("kickoff_at_oslo", "").strip()

    kickoff_utc = None
    if kickoff_str:
        try:
            local_dt = datetime.strptime(kickoff_str, "%Y-%m-%dT%H:%M")
            oslo_dt = OSLO_TZ.localize(local_dt)
            kickoff_utc = oslo_dt.astimezone(pytz.utc).replace(tzinfo=None)
        except Exception:
            pass

    from datetime import timedelta
    lock_utc = None
    if kickoff_utc:
        if phase == "group":
            lock_utc = kickoff_utc - timedelta(hours=24)
        elif phase == "knockout":
            lock_utc = kickoff_utc - timedelta(hours=24)

    match = Match(
        match_number=int(match_number) if match_number else 0,
        phase=phase,
        round_name=round_name,
        group_id=int(group_id) if group_id else None,
        home_team_id=int(home_team_id) if home_team_id else None,
        away_team_id=int(away_team_id) if away_team_id else None,
        kickoff_at_utc=kickoff_utc,
        lock_at_utc=lock_utc,
    )
    db.session.add(match)
    db.session.commit()
    flash("Kamp opprettet.", "success")
    return redirect(url_for("admin.tab", tab="groups"))


# ── Knockout ───────────────────────────────────────────────────────────────────

@admin_bp.route("/match/<int:match_id>/knockout", methods=["POST"])
def save_knockout_result(match_id):
    if not admin_required():
        abort(403)

    match = Match.query.get_or_404(match_id)
    home_score = request.form.get("home_score", "")
    away_score = request.form.get("away_score", "")
    is_finished = request.form.get("is_finished") == "1"
    result_note = request.form.get("result_note", "").strip()
    advanced_team_id = request.form.get("advanced_team_id")
    kickoff_str = request.form.get("kickoff_at_oslo", "").strip()
    manual_lock = request.form.get("manual_lock_override", "")

    if kickoff_str:
        try:
            local_dt = datetime.strptime(kickoff_str, "%Y-%m-%dT%H:%M")
            oslo_dt = OSLO_TZ.localize(local_dt)
            kickoff_utc = oslo_dt.astimezone(pytz.utc).replace(tzinfo=None)
            match.kickoff_at_utc = kickoff_utc
            match.kickoff_at_oslo_cache = kickoff_str.replace("T", " ")
            from datetime import timedelta
            match.lock_at_utc = kickoff_utc - timedelta(hours=24)
        except Exception:
            pass

    try:
        if home_score != "":
            match.home_score = int(home_score)
        if away_score != "":
            match.away_score = int(away_score)
    except (ValueError, TypeError):
        pass

    match.is_finished = is_finished
    match.result_note = result_note

    if manual_lock == "lock":
        match.manual_lock_override = True
    elif manual_lock == "open":
        match.manual_lock_override = False
    else:
        match.manual_lock_override = None

    # Auto-determine winner from score if clear (not a draw / penalties)
    if match.home_score is not None and match.away_score is not None:
        if match.home_score > match.away_score:
            advanced_team_id = str(match.home_team_id)
        elif match.away_score > match.home_score:
            advanced_team_id = str(match.away_team_id)
        # Equal: keep manually submitted advanced_team_id (penalties/extra time)

    if advanced_team_id:
        match.advanced_team_id = int(advanced_team_id)
        if match.home_team_id and match.away_team_id:
            if int(advanced_team_id) == match.home_team_id:
                match.loser_team_id = match.away_team_id
            else:
                match.loser_team_id = match.home_team_id

    db.session.commit()

    # Always propagate winner to next match (also when correcting an already-finished match)
    if match.advanced_team_id:
        advance_team_in_bracket(match.match_number, match.advanced_team_id, match.loser_team_id)

    recalculate_all_scores()
    db.session.add(AdminAuditLog(action="save_knockout_result", details=f"Kamp {match.match_number}"))
    db.session.commit()
    msg = f"Sluttspillkamp {match.match_number} oppdatert."
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        from flask import jsonify
        return jsonify({"ok": True, "flash": msg})
    flash(msg, "success")
    return redirect(url_for("admin.tab", tab="knockout"))


@admin_bp.route("/fill-round-of-32", methods=["POST"])
def trigger_fill_round_of_32():
    if not admin_required():
        abort(403)
    total_group = Match.query.filter_by(phase="group").count()
    finished_group = Match.query.filter_by(phase="group", is_finished=True).count()
    calculate_group_standings()
    calculate_third_place_rankings()
    fill_round_of_32()
    if finished_group < total_group:
        flash(
            f"Round of 32 delvis fylt ({finished_group} av {total_group} gruppekamper ferdig). "
            "Kjør på nytt når alle grupper er spilt.",
            "warning"
        )
    else:
        flash("Round of 32 fylt automatisk.", "success")
    return redirect(url_for("admin.tab", tab="knockout"))


# ── Users ──────────────────────────────────────────────────────────────────────

@admin_bp.route("/user/<int:user_id>/deactivate", methods=["POST"])
def deactivate_user(user_id):
    if not admin_required():
        abort(403)
    user = User.query.get_or_404(user_id)
    user.is_active = False
    db.session.commit()
    flash(f"Bruker {user.name} deaktivert.", "success")
    return redirect(url_for("admin.tab", tab="users"))


@admin_bp.route("/recalculate", methods=["POST"])
def recalculate():
    if not admin_required():
        abort(403)
    recalculate_all_scores()
    flash("Alle poeng reberegnet.", "success")
    return redirect(url_for("admin.tab", tab="users"))


@admin_bp.route("/reset-knockout", methods=["POST"])
def reset_knockout():
    if not admin_required():
        abort(403)

    # Reset all match result data for both phases
    all_matches = Match.query.all()
    for m in all_matches:
        m.home_score = None
        m.away_score = None
        m.is_finished = False
        m.advanced_team_id = None
        m.loser_team_id = None
        m.result_note = None
        m.is_locked = False
        m.manual_lock_override = None

        # Knockout team slots are rebuilt from prior matches and should be cleared
        if m.phase == "knockout":
            m.home_team_id = None
            m.away_team_id = None

    # Reset computed tables/caches
    from models import GroupStanding, ThirdPlaceRanking, ScoreCache, ScoreboardSnapshot
    GroupStanding.query.delete()
    ThirdPlaceRanking.query.delete()
    ScoreCache.query.delete()
    ScoreboardSnapshot.query.delete()

    db.session.commit()

    # Recreate standings rows with zeroed stats so group tables remain visible.
    calculate_group_standings()

    db.session.add(AdminAuditLog(action="reset_knockout", details="Full match reset (group + knockout) by admin"))
    db.session.commit()

    flash("Alle kamper nullstilt (gruppespill + sluttspill), inkludert resultater og avledede tabeller.", "success")
    return redirect(url_for("admin.tab", tab="groups"))


@admin_bp.route("/reset-user-tips", methods=["POST"])
def reset_user_tips():
    if not admin_required():
        abort(403)

    # Remove all submitted user tips and cached score artifacts.
    UserPrediction.query.delete()
    GroupPrediction.query.delete()
    ScoreCache.query.delete()
    ScoreboardSnapshot.query.delete()

    db.session.commit()

    db.session.add(AdminAuditLog(action="reset_user_tips", details="All user tips reset by admin"))
    db.session.commit()

    flash("All tipping fra brukere er nullstilt.", "success")
    return redirect(url_for("admin.tab", tab="settings"))


@admin_bp.route("/api/standings/<int:group_id>")
def api_standings(group_id):
    if not admin_required():
        abort(403)
    from models import GroupStanding
    standings = GroupStanding.query.filter_by(group_id=group_id).all()
    standings_sorted = sorted(standings, key=lambda s: s.rank or 99)
    rows = []
    for s in standings_sorted:
        rows.append({
            "name": s.team.name if s.team else "-",
            "team_id": s.team_id,
            "played": s.played,
            "wins": s.wins,
            "draws": s.draws,
            "losses": s.losses,
            "goals_for": s.goals_for,
            "goals_against": s.goals_against,
            "goal_difference": s.goal_difference,
            "points": s.points,
            "rank": s.rank or "-",
            "manual_rank_override": s.manual_rank_override or "",
        })
    from flask import jsonify
    return jsonify({"ok": True, "rows": rows, "group_id": group_id})


@admin_bp.route("/export/users.csv")
def export_users():
    if not admin_required():
        abort(403)
    data = export_users_csv()
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name="brukere.csv"
    )


# ── QR / Invitations ──────────────────────────────────────────────────────────

@admin_bp.route("/invite/generate", methods=["POST"])
def generate_invite():
    if not admin_required():
        abort(403)
    token = secrets.token_urlsafe(32)
    invite = InviteToken(token=token)
    db.session.add(invite)
    db.session.commit()
    db.session.add(AdminAuditLog(action="generate_invite", details=f"Token: {token[:8]}..."))
    db.session.commit()
    flash("Ny invitasjon generert.", "success")
    return redirect(url_for("admin.tab", tab="qr"))


@admin_bp.route("/invite/bulk/pdf", methods=["POST"])
def generate_invite_pdf():
    if not admin_required():
        abort(403)

    try:
        count = int(request.form.get("count", "1"))
    except (ValueError, TypeError):
        count = 1

    count = max(1, min(count, 200))

    base_url = request.host_url.rstrip("/")
    tokens = []
    for _ in range(count):
        token = secrets.token_urlsafe(32)
        invite = InviteToken(token=token)
        db.session.add(invite)
        tokens.append(token)
    db.session.commit()

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    for token in tokens:
        join_url = f"{base_url}/join/{token}"
        qr_img = qrcode.make(join_url)
        qr_buf = io.BytesIO()
        qr_img.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        qr_reader = ImageReader(qr_buf)

        qr_size = 320
        qr_x = (page_w - qr_size) / 2
        qr_y = (page_h - qr_size) / 2 + 80

        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawCentredString(page_w / 2, page_h - 80, "Invitasjon til VM 2026 Tipping")

        pdf.drawImage(qr_reader, qr_x, qr_y, width=qr_size, height=qr_size)

        pdf.setFont("Helvetica", 10)
        pdf.drawCentredString(page_w / 2, qr_y - 24, "Skann QR-koden for å delta")
        pdf.setFont("Helvetica", 8)
        pdf.drawCentredString(page_w / 2, qr_y - 40, join_url)

        pdf.showPage()

    pdf.save()
    buf.seek(0)

    db.session.add(AdminAuditLog(action="generate_invite_pdf", details=f"Count: {count}"))
    db.session.commit()

    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="invitasjoner_qr.pdf",
    )


@admin_bp.route("/invite/<int:invite_id>/qr")
def show_qr(invite_id):
    if not admin_required():
        abort(403)
    invite = InviteToken.query.get_or_404(invite_id)
    base_url = request.host_url.rstrip("/")
    join_url = f"{base_url}/join/{invite.token}"

    img = qrcode.make(join_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    return render_template("qr_view.html", invite=invite, join_url=join_url, qr_b64=img_b64)


@admin_bp.route("/invite/<int:invite_id>/deactivate", methods=["POST"])
def deactivate_invite(invite_id):
    if not admin_required():
        abort(403)
    invite = InviteToken.query.get_or_404(invite_id)
    invite.is_active = False
    db.session.commit()
    flash("Token deaktivert.", "success")
    return redirect(url_for("admin.tab", tab="qr"))


# ── Settings ──────────────────────────────────────────────────────────────────

@admin_bp.route("/settings/save", methods=["POST"])
def save_settings():
    if not admin_required():
        abort(403)
    keys = [
        "competition_name", "entry_fee", "group_stage_lock_at",
        "show_public_statistics", "show_scoreboard"
    ]
    for key in keys:
        val = request.form.get(key, "")
        CompetitionSetting.set(key, val)

    # Score settings
    for k in DEFAULT_SCORES:
        val = request.form.get(f"score_{k}", "")
        if val:
            row = ScoreSetting.query.filter_by(key=k).first()
            if row:
                row.value = val
            else:
                db.session.add(ScoreSetting(key=k, value=val))

    db.session.commit()
    flash("Innstillinger lagret.", "success")
    return redirect(url_for("admin.tab", tab="settings"))


# ── Schedule Import ────────────────────────────────────────────────────────────

@admin_bp.route("/schedule/import", methods=["POST"])
def import_schedule():
    if not admin_required():
        abort(403)

    file = request.files.get("schedule_file")
    if not file:
        flash("Ingen fil valgt.", "error")
        return redirect(url_for("admin.tab", tab="schedule"))

    content = file.read()
    dry_run = request.form.get("dry_run") == "1"

    result = import_schedule_from_csv(content, dry_run=dry_run)

    if dry_run:
        return render_template("import_preview.html",
            result=result,
            confirm_url=url_for("admin.import_schedule_confirm"),
            csv_content_b64=base64.b64encode(content).decode()
        )

    if result["success"]:
        flash(f"Import OK: {result['new_matches']} nye, {result['updated_matches']} oppdatert.", "success")
    else:
        for err in result["errors"]:
            flash(err, "error")

    return redirect(url_for("admin.tab", tab="schedule"))


@admin_bp.route("/schedule/import/confirm", methods=["POST"])
def import_schedule_confirm():
    if not admin_required():
        abort(403)
    csv_b64 = request.form.get("csv_content_b64", "")
    content = base64.b64decode(csv_b64)
    result = import_schedule_from_csv(content, dry_run=False)
    if result["success"]:
        flash(f"Import bekreftet: {result['new_matches']} nye, {result['updated_matches']} oppdatert.", "success")
    else:
        for err in result["errors"]:
            flash(err, "error")
    return redirect(url_for("admin.tab", tab="schedule"))


# ── Excel/CSV import for results ──────────────────────────────────────────────

@admin_bp.route("/results/import", methods=["POST"])
def import_results():
    if not admin_required():
        abort(403)

    file = request.files.get("results_file")
    if not file:
        flash("Ingen fil valgt.", "error")
        return redirect(url_for("admin.tab", tab="groups"))

    filename = file.filename or ""
    content = file.read()
    file_type = "xlsx" if filename.lower().endswith(".xlsx") else "csv"

    preview_result = validate_and_preview_import(content, file_type)
    if not preview_result["success"]:
        for err in preview_result["errors"]:
            flash(err, "error")
        return redirect(url_for("admin.tab", tab="groups"))

    import json
    rows_json = base64.b64encode(json.dumps(preview_result["rows"]).encode()).decode()
    return render_template("import_preview.html",
        result=preview_result,
        confirm_url=url_for("admin.import_results_confirm"),
        rows_json=rows_json,
        preview_rows=preview_result["rows"][:20],
    )


@admin_bp.route("/results/import/confirm", methods=["POST"])
def import_results_confirm():
    if not admin_required():
        abort(403)
    import json
    rows_json = request.form.get("rows_json", "")
    rows = json.loads(base64.b64decode(rows_json).decode())
    result = execute_import(rows)
    if result["success"]:
        flash(f"Import OK: {result['new']} nye, {result['updated']} oppdatert.", "success")
    else:
        for err in result["errors"]:
            flash(err, "error")
    return redirect(url_for("admin.tab", tab="groups"))


@admin_bp.route("/template/excel")
def download_excel_template():
    if not admin_required():
        abort(403)
    buf = generate_excel_template()
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="kampplan_mal.xlsx")


@admin_bp.route("/template/csv")
def download_csv_template():
    if not admin_required():
        abort(403)
    data = generate_csv_template()
    return send_file(io.BytesIO(data), mimetype="text/csv",
                     as_attachment=True, download_name="kampplan_mal.csv")


# ── Override group standings ───────────────────────────────────────────────────

@admin_bp.route("/standing/override", methods=["POST"])
def override_standing():
    if not admin_required():
        abort(403)
    group_id = request.form.get("group_id")
    team_id = request.form.get("team_id")
    rank = request.form.get("rank")
    from models import GroupStanding
    standing = GroupStanding.query.filter_by(group_id=int(group_id), team_id=int(team_id)).first()
    if standing:
        standing.manual_rank_override = int(rank) if rank else None
        db.session.commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            from flask import jsonify
            return jsonify({"ok": True, "flash": "Tabellplassering overstyrt."})
        flash("Tabellplassering overstyrt.", "success")
    return redirect(url_for("admin.tab", tab="groups"))
