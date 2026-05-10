"""
schedule_import.py - Import av offisiell kampplan fra CSV.
"""
import csv
import io
from datetime import datetime
import pytz
from models import db, Group, Team, Match, AdminAuditLog, CompetitionSetting, now_utc

OSLO_TZ = pytz.timezone("Europe/Oslo")
UTC_TZ = pytz.utc
ET_TZ = pytz.timezone("America/New_York")

REQUIRED_COLUMNS = [
    "match_number", "phase", "round_name", "kickoff_date_et", "kickoff_time_et",
    "home_slot", "away_slot",
]


def parse_kickoff(date_str, time_str, tz_str="America/New_York"):
    """Parse date+time string and convert to UTC datetime."""
    try:
        source_tz = pytz.timezone(tz_str)
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt_local = source_tz.localize(dt)
        return dt_local.astimezone(UTC_TZ).replace(tzinfo=None)
    except Exception:
        return None


def import_schedule_from_csv(csv_content, dry_run=False):
    """
    Import schedule from CSV content (string or bytes).
    Returns dict with results/errors.
    """
    if isinstance(csv_content, bytes):
        csv_content = csv_content.decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)

    errors = []
    warnings = []
    new_matches = []
    updated_matches = []
    new_teams = []
    new_groups = []

    # Validate columns
    if not rows:
        return {"success": False, "errors": ["CSV-filen er tom."]}

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing_cols:
        return {"success": False, "errors": [f"Mangler kolonner: {', '.join(missing_cols)}"]}

    # Validate match numbers unique
    match_numbers = [r.get("match_number") for r in rows]
    if len(match_numbers) != len(set(match_numbers)):
        return {"success": False, "errors": ["match_number er ikke unik i CSV-filen."]}

    # Validate counts
    group_rows = [r for r in rows if r.get("phase", "").strip().lower() == "group"]
    knockout_rows = [r for r in rows if r.get("phase", "").strip().lower() == "knockout"]

    if len(rows) != 104:
        errors.append(f"Forventet 104 kamper, fant {len(rows)}.")
    if len(group_rows) != 72:
        errors.append(f"Forventet 72 gruppespillkamper, fant {len(group_rows)}.")
    if len(knockout_rows) != 32:
        errors.append(f"Forventet 32 sluttspillkamper, fant {len(knockout_rows)}.")

    if errors:
        return {"success": False, "errors": errors}

    # Process rows
    group_cache = {}
    team_cache = {}

    for i, row in enumerate(rows):
        try:
            match_number = int(row["match_number"])
            phase = row.get("phase", "").strip().lower()
            round_name = row.get("round_name", "").strip()
            group_name = row.get("group_name", "").strip()
            home_slot = row.get("home_slot", "").strip()
            away_slot = row.get("away_slot", "").strip()
            home_team_code = row.get("home_team_code", "").strip()
            away_team_code = row.get("away_team_code", "").strip()
            home_team_name = row.get("home_team", "").strip()
            away_team_name = row.get("away_team", "").strip()
            venue = row.get("venue", "").strip()
            city = row.get("city", "").strip()
            country = row.get("country", "").strip()
            local_tz_str = row.get("local_timezone", "").strip()
            date_et = row.get("kickoff_date_et", "").strip()
            time_et = row.get("kickoff_time_et", "").strip()
            tz_src = row.get("kickoff_timezone_source", "America/New_York").strip() or "America/New_York"

            # Parse kickoff_at_utc - prefer explicit column if present
            kickoff_utc = None
            if row.get("kickoff_at_utc", "").strip():
                try:
                    raw = row["kickoff_at_utc"].strip()
                    kickoff_utc = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    pass
            if not kickoff_utc and date_et and time_et:
                kickoff_utc = parse_kickoff(date_et, time_et, tz_src)

            # Get/create group
            group_obj = None
            if group_name:
                if group_name not in group_cache:
                    if not dry_run:
                        grp = Group.query.filter_by(name=f"Gruppe {group_name}").first()
                        if not grp:
                            grp = Group(name=f"Gruppe {group_name}")
                            db.session.add(grp)
                            db.session.flush()
                            new_groups.append(group_name)
                        group_cache[group_name] = grp
                    else:
                        group_cache[group_name] = type("G", (), {"id": None, "name": f"Gruppe {group_name}"})()
                group_obj = group_cache[group_name]

            # Get/create teams
            home_team_id = None
            away_team_id = None
            if phase == "group":
                if home_team_code and home_team_name:
                    if home_team_code not in team_cache:
                        if not dry_run:
                            team = Team.query.filter_by(fifa_code=home_team_code).first()
                            if not team:
                                team = Team(name=home_team_name, fifa_code=home_team_code,
                                            group_id=group_obj.id if group_obj else None)
                                db.session.add(team)
                                db.session.flush()
                                new_teams.append(home_team_name)
                            team_cache[home_team_code] = team
                        else:
                            team_cache[home_team_code] = type("T", (), {"id": None})()
                    home_team_id = team_cache[home_team_code].id if not dry_run else None

                if away_team_code and away_team_name:
                    if away_team_code not in team_cache:
                        if not dry_run:
                            team = Team.query.filter_by(fifa_code=away_team_code).first()
                            if not team:
                                team = Team(name=away_team_name, fifa_code=away_team_code,
                                            group_id=group_obj.id if group_obj else None)
                                db.session.add(team)
                                db.session.flush()
                                new_teams.append(away_team_name)
                            team_cache[away_team_code] = team
                        else:
                            team_cache[away_team_code] = type("T", (), {"id": None})()
                    away_team_id = team_cache[away_team_code].id if not dry_run else None

            # Calculate lock_at_utc
            lock_at_utc = None
            if phase == "group":
                # Will be set globally after all rows processed
                pass
            elif kickoff_utc:
                from datetime import timedelta
                lock_at_utc = kickoff_utc - timedelta(hours=24) if kickoff_utc else None
                # Convert to naive UTC
                if lock_at_utc and hasattr(lock_at_utc, 'tzinfo') and lock_at_utc.tzinfo:
                    lock_at_utc = lock_at_utc.replace(tzinfo=None)

            # Oslo cache
            oslo_cache = None
            if kickoff_utc:
                try:
                    utc_aware = pytz.utc.localize(kickoff_utc)
                    oslo_dt = utc_aware.astimezone(OSLO_TZ)
                    oslo_cache = oslo_dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            match_data = {
                "match_number": match_number,
                "phase": phase,
                "round_name": round_name,
                "group_id": group_obj.id if group_obj and not dry_run else None,
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "home_slot_source": home_slot,
                "away_slot_source": away_slot,
                "kickoff_at_utc": kickoff_utc,
                "kickoff_timezone_source": tz_src,
                "kickoff_at_oslo_cache": oslo_cache,
                "local_timezone": local_tz_str,
                "venue": venue,
                "city": city,
                "country": country,
                "lock_at_utc": lock_at_utc,
            }

            if dry_run:
                existing = Match.query.filter_by(match_number=match_number).first()
                if existing:
                    updated_matches.append(match_number)
                else:
                    new_matches.append(match_number)
            else:
                existing = Match.query.filter_by(match_number=match_number).first()
                if existing:
                    for k, v in match_data.items():
                        setattr(existing, k, v)
                    updated_matches.append(match_number)
                else:
                    new = Match(**match_data)
                    db.session.add(new)
                    new_matches.append(match_number)

        except Exception as e:
            errors.append(f"Rad {i + 1} (kamp {row.get('match_number', '?')}): {e}")

    if errors:
        if not dry_run:
            db.session.rollback()
        return {"success": False, "errors": errors}

    if not dry_run:
        # Set per-match group lock_at_utc: 24h before each group match kickoff
        from datetime import timedelta
        group_matches = Match.query.filter_by(phase="group").all()
        for gm in group_matches:
            if gm.kickoff_at_utc:
                gm.lock_at_utc = gm.kickoff_at_utc - timedelta(hours=24)

        # Keep legacy setting for group-position tips lock (based on first group match)
        first_group = Match.query.filter_by(phase="group").order_by(Match.kickoff_at_utc).first()
        if first_group and first_group.kickoff_at_utc:
            group_lock = first_group.kickoff_at_utc - timedelta(hours=24)
            lock_str = group_lock.strftime("%Y-%m-%dT%H:%M:%S")
            CompetitionSetting.set("group_stage_lock_at", lock_str)

        db.session.commit()
        db.session.add(AdminAuditLog(
            action="import_schedule_csv",
            details=f"Nye: {len(new_matches)}, oppdatert: {len(updated_matches)}, grupper: {len(new_groups)}, lag: {len(new_teams)}"
        ))
        db.session.commit()

    return {
        "success": True,
        "errors": [],
        "warnings": warnings,
        "new_matches": len(new_matches),
        "updated_matches": len(updated_matches),
        "new_teams": len(new_teams),
        "new_groups": len(new_groups),
        "dry_run": dry_run,
    }
