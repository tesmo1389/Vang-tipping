"""
bracket.py - Sluttspillbracket-logikk for VM 2026 tipping.
Håndterer automatisk innlegging av lag i neste kamp basert på bracket_edges-tabellen.
"""
from models import db, Match, Team, BracketEdge, Group, GroupStanding, ThirdPlaceRanking, AdminAuditLog, now_utc


def advance_team_in_bracket(match_number, advanced_team_id, loser_team_id=None):
    """
    Når et lag er kåret som vinner av en kamp, legg laget inn i neste kamp
    basert på bracket_edges-tabellen.
    """
    edges = BracketEdge.query.filter_by(
        source_match_number=match_number,
        source_outcome="winner"
    ).all()

    for edge in edges:
        target = Match.query.filter_by(match_number=edge.target_match_number).first()
        if target:
            if edge.target_slot == "home":
                target.home_team_id = advanced_team_id
            elif edge.target_slot == "away":
                target.away_team_id = advanced_team_id

    # Handle loser (for third place match)
    if loser_team_id:
        loser_edges = BracketEdge.query.filter_by(
            source_match_number=match_number,
            source_outcome="loser"
        ).all()
        for edge in loser_edges:
            target = Match.query.filter_by(match_number=edge.target_match_number).first()
            if target:
                if edge.target_slot == "home":
                    target.home_team_id = loser_team_id
                elif edge.target_slot == "away":
                    target.away_team_id = loser_team_id

    db.session.commit()


def fill_round_of_32():
    """
    Fill Round of 32 matches with group winners, runners-up, and best third-placed teams.
    Should be called after all group stage matches are finished.
    """
    # Direct slots: winners and runners-up
    direct_slots = {
        # match_number: (slot_source, rank, group_letter)
        73: [("home", 2, "A"), ("away", 2, "B")],
        75: [("home", 1, "F"), ("away", 2, "C")],
        76: [("home", 1, "C"), ("away", 2, "F")],
        78: [("home", 2, "E"), ("away", 2, "I")],
        83: [("home", 2, "K"), ("away", 2, "L")],
        84: [("home", 1, "H"), ("away", 2, "J")],
        86: [("home", 1, "J"), ("away", 2, "H")],
        88: [("home", 2, "D"), ("away", 2, "G")],
    }

    group_map = {g.name.split()[-1]: g for g in Group.query.all()}

    def get_team_by_rank(group_letter, rank):
        grp = group_map.get(group_letter)
        if not grp:
            return None
        standings = GroupStanding.query.filter_by(group_id=grp.id).all()
        ranked = sorted(standings, key=lambda s: s.rank or 99)
        if len(ranked) >= rank:
            return ranked[rank - 1].team_id
        return None

    for match_num, slots in direct_slots.items():
        match = Match.query.filter_by(match_number=match_num).first()
        if not match:
            continue
        for slot, rank, group_letter in slots:
            team_id = get_team_by_rank(group_letter, rank)
            if team_id:
                if slot == "home":
                    match.home_team_id = team_id
                else:
                    match.away_team_id = team_id

    # Third-place slots
    _fill_third_place_slots(group_map)

    db.session.commit()
    db.session.add(AdminAuditLog(action="fill_round_of_32", details="Round of 32 filled automatically"))
    db.session.commit()


def _fill_third_place_slots(group_map):
    """Fill third-place slots in Round of 32 using assignment rules."""
    from models import ThirdPlaceAssignmentRule

    # Get the 8 best third-placed teams
    third = ThirdPlaceRanking.query.filter_by(qualified=True).order_by(ThirdPlaceRanking.rank).limit(8).all()
    if len(third) < 8:
        return

    qualified_group_letters = sorted([
        grp.name.split()[-1]
        for t in third
        for grp in [Group.query.get(t.group_id)]
        if grp
    ])
    key = ",".join(qualified_group_letters)

    rule = ThirdPlaceAssignmentRule.query.filter_by(qualified_groups_key=key).first()
    if not rule:
        return

    # Build map: group_letter -> team_id for third-placed teams
    third_map = {}
    for t in third:
        grp = Group.query.get(t.group_id)
        if grp:
            letter = grp.name.split()[-1]
            third_map[letter] = t.team_id

    third_match_slots = {
        74: ("away", rule.match_74_group),
        77: ("away", rule.match_77_group),
        79: ("away", rule.match_79_group),
        80: ("away", rule.match_80_group),
        81: ("away", rule.match_81_group),
        82: ("away", rule.match_82_group),
        85: ("away", rule.match_85_group),
        87: ("away", rule.match_87_group),
    }

    # Also handle home slots for matches 74, 77, 79, 80, 81, 82, 85, 87
    home_slots = {
        74: (1, "E"),
        77: (1, "I"),
        79: (1, "A"),
        80: (1, "L"),
        81: (1, "D"),
        82: (1, "G"),
        85: (1, "B"),
        87: (1, "K"),
    }

    def get_team_by_rank(group_letter, rank):
        grp = group_map.get(group_letter)
        if not grp:
            return None
        standings = GroupStanding.query.filter_by(group_id=grp.id).all()
        ranked = sorted(standings, key=lambda s: s.rank or 99)
        if len(ranked) >= rank:
            return ranked[rank - 1].team_id
        return None

    for match_num, (slot, group_letter) in third_match_slots.items():
        match = Match.query.filter_by(match_number=match_num).first()
        if not match or not group_letter:
            continue
        team_id = third_map.get(group_letter)
        if team_id:
            if slot == "home":
                match.home_team_id = team_id
            else:
                match.away_team_id = team_id

    # Fill home slots for these matches
    for match_num, (rank, group_letter) in home_slots.items():
        match = Match.query.filter_by(match_number=match_num).first()
        if not match:
            continue
        team_id = get_team_by_rank(group_letter, rank)
        if team_id:
            match.home_team_id = team_id


def calculate_group_standings():
    """Recalculate group standings from finished group matches."""
    from models import Group, GroupStanding, Match, Team

    groups = Group.query.all()
    for group in groups:
        teams = Team.query.filter_by(group_id=group.id).all()
        team_stats = {t.id: {
            "played": 0, "wins": 0, "draws": 0, "losses": 0,
            "goals_for": 0, "goals_against": 0, "points": 0
        } for t in teams}

        matches = Match.query.filter_by(group_id=group.id, is_finished=True).all()
        for match in matches:
            if match.home_score is None or match.away_score is None:
                continue
            h = match.home_team_id
            a = match.away_team_id
            hs = match.home_score
            as_ = match.away_score

            if h in team_stats:
                team_stats[h]["played"] += 1
                team_stats[h]["goals_for"] += hs
                team_stats[h]["goals_against"] += as_
                if hs > as_:
                    team_stats[h]["wins"] += 1
                    team_stats[h]["points"] += 3
                elif hs == as_:
                    team_stats[h]["draws"] += 1
                    team_stats[h]["points"] += 1
                else:
                    team_stats[h]["losses"] += 1

            if a in team_stats:
                team_stats[a]["played"] += 1
                team_stats[a]["goals_for"] += as_
                team_stats[a]["goals_against"] += hs
                if as_ > hs:
                    team_stats[a]["wins"] += 1
                    team_stats[a]["points"] += 3
                elif hs == as_:
                    team_stats[a]["draws"] += 1
                    team_stats[a]["points"] += 1
                else:
                    team_stats[a]["losses"] += 1

        # Rank teams
        def sort_key(item):
            tid, s = item
            gd = s["goals_for"] - s["goals_against"]
            return (-s["points"], -gd, -s["goals_for"])

        ranked = sorted(team_stats.items(), key=sort_key)

        for rank, (team_id, s) in enumerate(ranked, 1):
            standing = GroupStanding.query.filter_by(group_id=group.id, team_id=team_id).first()
            gd = s["goals_for"] - s["goals_against"]
            if standing:
                standing.played = s["played"]
                standing.wins = s["wins"]
                standing.draws = s["draws"]
                standing.losses = s["losses"]
                standing.goals_for = s["goals_for"]
                standing.goals_against = s["goals_against"]
                standing.goal_difference = gd
                standing.points = s["points"]
                standing.calculated_rank = rank
                standing.updated_at = now_utc()
            else:
                db.session.add(GroupStanding(
                    group_id=group.id,
                    team_id=team_id,
                    played=s["played"],
                    wins=s["wins"],
                    draws=s["draws"],
                    losses=s["losses"],
                    goals_for=s["goals_for"],
                    goals_against=s["goals_against"],
                    goal_difference=gd,
                    points=s["points"],
                    calculated_rank=rank,
                ))

    db.session.commit()


def calculate_third_place_rankings():
    """Rank all 12 third-placed teams and mark the best 8 as qualified."""
    from models import Group, GroupStanding, ThirdPlaceRanking, Team

    groups = Group.query.all()
    third_teams = []

    for group in groups:
        standings = GroupStanding.query.filter_by(group_id=group.id).all()
        ranked = sorted(standings, key=lambda s: s.rank or 99)
        if len(ranked) >= 3:
            third = ranked[2]
            third_teams.append({
                "group_id": group.id,
                "team_id": third.team_id,
                "points": third.points,
                "goal_difference": third.goal_difference,
                "goals_for": third.goals_for,
                "fair_play_score": third.fair_play_score,
            })

    # Sort third-place teams
    third_teams.sort(key=lambda x: (-x["points"], -x["goal_difference"], -x["goals_for"], -x["fair_play_score"]))

    for rank, t in enumerate(third_teams, 1):
        tpr = ThirdPlaceRanking.query.filter_by(group_id=t["group_id"], team_id=t["team_id"]).first()
        if tpr:
            tpr.rank = rank
            tpr.points = t["points"]
            tpr.goal_difference = t["goal_difference"]
            tpr.goals_for = t["goals_for"]
            tpr.qualified = rank <= 8
            tpr.updated_at = now_utc()
        else:
            db.session.add(ThirdPlaceRanking(
                group_id=t["group_id"],
                team_id=t["team_id"],
                rank=rank,
                points=t["points"],
                goal_difference=t["goal_difference"],
                goals_for=t["goals_for"],
                qualified=rank <= 8,
            ))

    db.session.commit()
