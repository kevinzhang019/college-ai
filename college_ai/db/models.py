"""SQLAlchemy ORM models for admissions data."""

from sqlalchemy import Column, Integer, Float, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class School(Base):
    __tablename__ = "schools"

    id = Column(Integer, primary_key=True)  # College Scorecard UNITID
    name = Column(Text, nullable=False)
    city = Column(Text)
    state = Column(Text)
    ownership = Column(Integer)  # 1=public, 2=private nonprofit, 3=for-profit
    acceptance_rate = Column(Float)
    sat_avg = Column(Float)
    sat_25 = Column(Float)
    sat_75 = Column(Float)
    act_25 = Column(Float)
    act_75 = Column(Float)
    enrollment = Column(Integer)
    retention_rate = Column(Float)
    graduation_rate = Column(Float)
    median_earnings_10yr = Column(Integer)
    tuition_in_state = Column(Integer)
    tuition_out_of_state = Column(Integer)
    student_faculty_ratio = Column(Float)
    pct_white = Column(Float)
    pct_black = Column(Float)
    pct_hispanic = Column(Float)
    pct_asian = Column(Float)
    pct_first_gen = Column(Float)
    yield_rate = Column(Float)
    updated_at = Column(Text)

    datapoints = relationship("ApplicantDatapoint", back_populates="school")
    niche_grade = relationship("NicheGrade", back_populates="school", uselist=False)


class ApplicantDatapoint(Base):
    __tablename__ = "applicant_datapoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False)
    source = Column(Text, nullable=False)  # 'niche'
    gpa = Column(Float)
    sat_score = Column(Float)
    act_score = Column(Float)
    outcome = Column(Text, nullable=False)  # 'accepted' | 'rejected' | 'waitlisted'
    residency = Column(Text)        # 'inState' | 'outOfState'
    major = Column(Text)            # intended major / field of study
    scraped_at = Column(Text)

    school = relationship("School", back_populates="datapoints")


class NicheGrade(Base):
    __tablename__ = "niche_grades"

    school_id = Column(Integer, ForeignKey("schools.id"), primary_key=True)

    # Overall Niche grade and rank
    overall_grade = Column(Text)         # e.g. "A+", "A", "B+"
    niche_rank = Column(Integer)         # Overall national rank (e.g. 3)

    # Category letter grades
    academics = Column(Text)
    value = Column(Text)
    diversity = Column(Text)
    campus = Column(Text)
    athletics = Column(Text)
    party_scene = Column(Text)
    professors = Column(Text)
    location = Column(Text)
    dorms = Column(Text)
    food = Column(Text)
    student_life = Column(Text)
    safety = Column(Text)

    # Quantitative stats from Niche
    acceptance_rate_niche = Column(Float)       # e.g. 0.04
    avg_annual_cost = Column(Integer)           # net price in USD
    graduation_rate_niche = Column(Float)       # e.g. 0.95
    student_faculty_ratio_niche = Column(Float) # e.g. 5.0
    setting = Column(Text)                      # City / Suburb / Town / Rural
    religious_affiliation = Column(Text)        # e.g. "Catholic", "None"
    pct_students_on_campus = Column(Float)      # e.g. 0.93
    pct_greek_life = Column(Float)              # e.g. 0.15
    avg_rating = Column(Float)                  # Niche user star rating 1-5
    review_count = Column(Integer)

    no_data = Column(Integer, default=0)        # 1 = confirmed no Niche data for this school

    updated_at = Column(Text)

    school = relationship("School", back_populates="niche_grade")


