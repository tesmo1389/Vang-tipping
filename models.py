"""
models.py - SQLAlchemy database models for VM 2026 tipping app.
"""
from datetime import datetime, timezone, timedelta
import pytz
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

OSLO_TZ = pytz.timezone("Europe/Oslo")


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Group(db.Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)  # "Gruppe A"
    color = db.Column(db.String(20), default="#4a90e2")

    teams = db.relationship("Team", back_populates="group", lazy="dynamic")
    matches = db.relationship("Match", back_populates="group", lazy="dynamic")
    standings = db.relationship("GroupStanding", back_populates="group", lazy="dynamic")


class Team(db.Model):
    __tablename__ = "teams"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    fifa_code = db.Column(db.String(10), unique=True, nullable=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    group_slot = db.Column(db.Integer, nullable=True)  # 1-4
    fifa_ranking = db.Column(db.Integer, nullable=True)
    flag_emoji = db.Column(db.String(10), nullable=True)

    group = db.relationship("Group", back_populates="teams")


class Match(db.Model):
    __tablename__ = "matches"
    id = db.Column(db.Integer, primary_key=True)
    match_number = db.Column(db.Integer, unique=True, nullable=False)
    phase = db.Column(db.String(20), nullable=False)  # group / knockout
    round_name = db.Column(db.String(30), nullable=False)  # group_stage, round_of_32, ...
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)

    home_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    away_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    home_slot_source = db.Column(db.String(20), nullable=True)  # e.g. "1A", "W73"
    away_slot_source = db.Column(db.String(20), nullable=True)

    kickoff_at_utc = db.Column(db.DateTime, nullable=True)
    kickoff_timezone_source = db.Column(db.String(50), nullable=True)
    kickoff_at_oslo_cache = db.Column(db.String(30), nullable=True)
    local_timezone = db.Column(db.String(50), nullable=True)
    venue = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    country = db.Column(db.String(100), nullable=True)

    lock_at_utc = db.Column(db.DateTime, nullable=True)
    home_score = db.Column(db.Integer, nullable=True)
    away_score = db.Column(db.Integer, nullable=True)
    advanced_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    loser_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    result_note = db.Column(db.String(200), nullable=True)
    is_finished = db.Column(db.Boolean, default=False)
    is_locked = db.Column(db.Boolean, default=False)
    manual_lock_override = db.Column(db.Boolean, nullable=True)  # None=auto, True=force lock, False=force open

    group = db.relationship("Group", back_populates="matches")
    home_team = db.relationship("Team", foreign_keys=[home_team_id])
    away_team = db.relationship("Team", foreign_keys=[away_team_id])
    advanced_team = db.relationship("Team", foreign_keys=[advanced_team_id])
    loser_team = db.relationship("Team", foreign_keys=[loser_team_id])

    @property
    def kickoff_oslo(self):
        if self.kickoff_at_utc:
            utc_dt = self.kickoff_at_utc.replace(tzinfo=pytz.utc)
            return utc_dt.astimezone(OSLO_TZ)
        return None

    @property
    def effective_lock_at_utc(self):
        """Return effective lock time for this match."""
        if self.lock_at_utc:
            return self.lock_at_utc
        if not self.kickoff_at_utc:
            return None
        if self.phase == "group":
            return self.kickoff_at_utc - timedelta(hours=1)
        if self.phase == "knockout":
            return self.kickoff_at_utc - timedelta(hours=1)
        return None

    @property
    def effective_locked(self):
        """Check if match is effectively locked."""
        if self.manual_lock_override is True:
            return True
        if self.manual_lock_override is False:
            return False
        if self.is_locked:
            return True
        lock_at = self.effective_lock_at_utc
        if lock_at:
            return now_utc() >= lock_at
        return False

    @property
    def is_open_for_betting(self):
        if self.is_finished:
            return False
        if self.effective_locked:
            return False
        if self.phase == "knockout":
            if not self.home_team_id or not self.away_team_id:
                return False
            if not self.kickoff_at_utc:
                return False
        return True

    def get_result_str(self):
        if self.home_score is not None and self.away_score is not None:
            return f"{self.home_score}–{self.away_score}"
        return None


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200), nullable=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    has_paid = db.Column(db.Boolean, default=False)
    anonymous_label = db.Column(db.String(50), nullable=True)  # "Spiller 1" etc.

    predictions = db.relationship("UserPrediction", back_populates="user", lazy="dynamic")
    group_predictions = db.relationship("GroupPrediction", back_populates="user", lazy="dynamic")
    score_caches = db.relationship("ScoreCache", back_populates="user", lazy="dynamic")


class InviteToken(db.Model):
    __tablename__ = "invite_tokens"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    used_at = db.Column(db.DateTime, nullable=True)
    used_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    used_by_user = db.relationship("User", foreign_keys=[used_by_user_id])


class UserPrediction(db.Model):
    __tablename__ = "user_predictions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    match_id = db.Column(db.Integer, db.ForeignKey("matches.id"), nullable=False)
    predicted_home_score = db.Column(db.Integer, nullable=True)
    predicted_away_score = db.Column(db.Integer, nullable=True)
    predicted_hub = db.Column(db.String(1), nullable=True)  # H/U/B
    predicted_winner_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    predicted_advanced_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc)
    submitted_at = db.Column(db.DateTime, default=now_utc)
    is_valid = db.Column(db.Boolean, default=True)

    user = db.relationship("User", back_populates="predictions")
    match = db.relationship("Match")
    predicted_winner_team = db.relationship("Team", foreign_keys=[predicted_winner_team_id])
    predicted_advanced_team = db.relationship("Team", foreign_keys=[predicted_advanced_team_id])

    __table_args__ = (db.UniqueConstraint("user_id", "match_id", name="uix_user_match"),)


class GroupPrediction(db.Model):
    __tablename__ = "group_predictions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    predicted_winner_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    predicted_second_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    predicted_third_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc)

    user = db.relationship("User", back_populates="group_predictions")
    group = db.relationship("Group")
    predicted_winner = db.relationship("Team", foreign_keys=[predicted_winner_team_id])
    predicted_second = db.relationship("Team", foreign_keys=[predicted_second_team_id])
    predicted_third = db.relationship("Team", foreign_keys=[predicted_third_team_id])

    __table_args__ = (db.UniqueConstraint("user_id", "group_id", name="uix_user_group"),)


class GroupStanding(db.Model):
    __tablename__ = "group_standings"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    calculated_rank = db.Column(db.Integer, nullable=True)
    manual_rank_override = db.Column(db.Integer, nullable=True)
    played = db.Column(db.Integer, default=0)
    wins = db.Column(db.Integer, default=0)
    draws = db.Column(db.Integer, default=0)
    losses = db.Column(db.Integer, default=0)
    goals_for = db.Column(db.Integer, default=0)
    goals_against = db.Column(db.Integer, default=0)
    goal_difference = db.Column(db.Integer, default=0)
    points = db.Column(db.Integer, default=0)
    fair_play_score = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc)

    group = db.relationship("Group", back_populates="standings")
    team = db.relationship("Team")

    __table_args__ = (db.UniqueConstraint("group_id", "team_id", name="uix_group_team"),)

    @property
    def rank(self):
        return self.manual_rank_override if self.manual_rank_override is not None else self.calculated_rank


class ThirdPlaceRanking(db.Model):
    __tablename__ = "third_place_rankings"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    rank = db.Column(db.Integer, nullable=True)
    points = db.Column(db.Integer, default=0)
    goal_difference = db.Column(db.Integer, default=0)
    goals_for = db.Column(db.Integer, default=0)
    fair_play_score = db.Column(db.Integer, default=0)
    fifa_ranking = db.Column(db.Integer, nullable=True)
    qualified = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc)

    group = db.relationship("Group")
    team = db.relationship("Team")

    __table_args__ = (db.UniqueConstraint("group_id", "team_id", name="uix_tpr_group_team"),)


class ThirdPlaceAssignmentRule(db.Model):
    __tablename__ = "third_place_assignment_rules"
    id = db.Column(db.Integer, primary_key=True)
    qualified_groups_key = db.Column(db.String(30), unique=True, nullable=False)
    match_74_group = db.Column(db.String(2), nullable=True)
    match_77_group = db.Column(db.String(2), nullable=True)
    match_79_group = db.Column(db.String(2), nullable=True)
    match_80_group = db.Column(db.String(2), nullable=True)
    match_81_group = db.Column(db.String(2), nullable=True)
    match_82_group = db.Column(db.String(2), nullable=True)
    match_85_group = db.Column(db.String(2), nullable=True)
    match_87_group = db.Column(db.String(2), nullable=True)


class BracketEdge(db.Model):
    __tablename__ = "bracket_edges"
    id = db.Column(db.Integer, primary_key=True)
    source_match_number = db.Column(db.Integer, nullable=False)
    source_outcome = db.Column(db.String(10), nullable=False)  # winner / loser
    target_match_number = db.Column(db.Integer, nullable=False)
    target_slot = db.Column(db.String(10), nullable=False)  # home / away


class ScoreSetting(db.Model):
    __tablename__ = "score_settings"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(50), nullable=False)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        if row:
            try:
                return int(row.value)
            except (ValueError, TypeError):
                return row.value
        return default


class CompetitionSetting(db.Model):
    __tablename__ = "competition_settings"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))


class ScoreCache(db.Model):
    __tablename__ = "score_cache"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    points = db.Column(db.Integer, default=0)
    recalculated_at = db.Column(db.DateTime, default=now_utc)

    user = db.relationship("User", back_populates="score_caches")

    __table_args__ = (db.UniqueConstraint("user_id", "category", name="uix_score_user_cat"),)


class ScoreboardSnapshot(db.Model):
    __tablename__ = "scoreboard_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    rank = db.Column(db.Integer, nullable=True)
    points = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=now_utc)

    user = db.relationship("User")


class AdminAuditLog(db.Model):
    __tablename__ = "admin_audit_log"
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc)
