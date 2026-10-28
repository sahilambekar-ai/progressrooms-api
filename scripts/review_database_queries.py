"""Synthetic PostgreSQL query review; never reuses application tables.

Run with APP_ENV=test, DATABASE_REVIEW_POSTGRES_URL pointing to a dedicated test
DB, and --output /path/plans.json. Creates/drops one random schema. Synthetic
sizes are fixed so this is a bounded plan experiment, not a load benchmark.
"""
import argparse
import json
import os
from pathlib import Path
import uuid
from datetime import timedelta

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.models.support import SupportTicket
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment, Order, Payment
from app.models.communication import MeetingEmailDelivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    url = os.environ['DATABASE_REVIEW_POSTGRES_URL'].replace('postgresql+asyncpg:', 'postgresql:')
    schema = 'review_queries_' + uuid.uuid4().hex
    root = sa.create_engine(url)
    engine = sa.create_engine(url, connect_args={'options':f'-csearch_path={schema}'})
    with root.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    try:
        with engine.connect() as conn:
            config = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
            config.attributes['connection'] = conn
            command.upgrade(config, 'head')
            statements = [
                "INSERT INTO organizations (id,name,slug,status,timezone,currency,created_at,updated_at) SELECT md5('org-'||i)::uuid,'Studio '||i,'studio-'||i,'ACTIVE','UTC','INR',now(),now() FROM generate_series(1,100) i",
                "INSERT INTO users (id,email,full_name,role,is_active,is_superadmin,is_verified,account_status,created_at,updated_at) SELECT md5('user-'||i)::uuid,'u'||i||'@example.test','Student '||i,'STUDENT',true,false,true,'ACTIVE',now(),now() FROM generate_series(1,10000) i",
                "INSERT INTO class_types (id,organization_id,name,slug,created_at,updated_at) SELECT md5('type-'||i)::uuid,md5('org-'||i)::uuid,'Yoga','yoga',now(),now() FROM generate_series(1,100) i",
                "INSERT INTO sessions (id,organization_id,class_type_id,name,slug,session_type,skill_level,duration_minutes,language,is_active,is_public,created_at,updated_at) SELECT md5('class-'||i)::uuid,md5('org-'||((i-1)/20+1))::uuid,md5('type-'||((i-1)/20+1))::uuid,'Yoga '||i,'class-'||i,'GROUP','ALL_LEVELS',60,'English',true,true,now(),now() FROM generate_series(1,2000) i",
                "INSERT INTO support_tickets (id,organization_id,student_id,student_name,student_email,subject,status,created_at,updated_at) SELECT md5('ticket-'||i)::uuid,md5('org-'||((i-1)/1000+1))::uuid,md5('user-'||((i-1)/10+1))::uuid,'Student','synthetic@example.test','Synthetic question',CASE WHEN i%5=0 THEN 'OPEN' ELSE 'RESOLVED' END,now()-i*interval '1 second',now()-i*interval '1 second' FROM generate_series(1,100000) i",
                "INSERT INTO class_occurrences (id,organization_id,session_id,scheduled_start_at,scheduled_end_at,actual_start_at,actual_end_at,status,zoom_provisioning_status,created_at,updated_at) SELECT md5('occ-'||i)::uuid,md5('org-'||((i-1)/1000+1))::uuid,md5('class-'||((i-1)/50+1))::uuid,now()+(i%100)*interval '1 day',now()+(i%100)*interval '1 day'+interval '1 hour',now()+(i%100)*interval '1 day',now()+(i%100)*interval '1 day'+interval '1 hour','SCHEDULED','NOT_PROVISIONED',now(),now() FROM generate_series(1,100000) i",
                "INSERT INTO session_enrollments (id,organization_id,session_id,user_id,status,valid_from,valid_until,sessions_attended,created_at,updated_at) SELECT md5('enrollment-'||i)::uuid,md5('org-'||((i-1)/1000+1))::uuid,md5('class-'||((i-1)/50+1))::uuid,md5('user-'||((i-1)/10+1))::uuid,'ACTIVE',now()-interval '30 days',now()+interval '30 days',0,now(),now() FROM generate_series(1,100000) i",
                "INSERT INTO orders (id,organization_id,user_id,order_number,total_amount,currency,status,created_at,updated_at) SELECT md5('order-'||i)::uuid,md5('org-'||((i-1)/500+1))::uuid,md5('user-'||((i-1)/5+1))::uuid,'SYN-'||i,500,'INR','PAID',now(),now() FROM generate_series(1,50000) i",
                "INSERT INTO payments (id,order_id,amount,currency,payment_provider,status,created_at,updated_at) SELECT md5('payment-'||i)::uuid,md5('order-'||i)::uuid,500,'INR','RAZORPAY','SUCCESS',now(),now() FROM generate_series(1,50000) i",
                "INSERT INTO meeting_email_deliveries (id,class_occurrence_id,recipient_user_id,event_key,message_type,scheduled_start_at,status,attempts,available_at,created_at,updated_at) SELECT md5('mail-'||i)::uuid,md5('occ-'||i)::uuid,md5('user-'||((i-1)/10+1))::uuid,'synthetic-'||i,'INVITATION',now(),CASE WHEN i%100=0 THEN 'PENDING' ELSE 'SENT' END,0,now()-i*interval '1 second',now(),now() FROM generate_series(1,100000) i",
            ]
            for statement in statements:
                conn.execute(sa.text(statement))
            conn.commit()
            conn.execute(sa.text('ANALYZE'))
            org = uuid.UUID(bytes=__import__('hashlib').md5(b'org-1').digest())
            student = uuid.UUID(bytes=__import__('hashlib').md5(b'user-1').digest())
            session = uuid.UUID(bytes=__import__('hashlib').md5(b'class-1').digest())
            now = conn.scalar(sa.text('SELECT now()'))
            queries = {
                'student_support':sa.select(SupportTicket).where(SupportTicket.student_id==student).order_by(SupportTicket.updated_at.desc()),
                'studio_open_support':sa.select(SupportTicket).where(SupportTicket.organization_id==org,SupportTicket.status=='OPEN').order_by(SupportTicket.updated_at.desc()),
                'studio_month_schedule':sa.select(ClassOccurrence).where(ClassOccurrence.organization_id==org,ClassOccurrence.actual_start_at>=now,ClassOccurrence.actual_start_at<=now+timedelta(days=30)).order_by(ClassOccurrence.actual_start_at),
                'session_active_enrollment_count':sa.select(sa.func.count()).select_from(SessionEnrollment).where(SessionEnrollment.session_id==session,SessionEnrollment.status=='ACTIVE'),
                'student_studio_enrollments':sa.select(SessionEnrollment).where(SessionEnrollment.organization_id==org,SessionEnrollment.user_id==student),
                'studio_collected_payments':sa.select(sa.func.sum(Payment.amount)).join(Order,Payment.order_id==Order.id).where(Order.organization_id==org,Order.currency=='INR',Order.status=='PAID',Payment.status=='SUCCESS',Payment.currency=='INR'),
                'due_meeting_email_batch':sa.select(MeetingEmailDelivery.id).where(MeetingEmailDelivery.status.in_(['PENDING','RETRY']),MeetingEmailDelivery.available_at<=now).order_by(MeetingEmailDelivery.available_at).limit(100),
            }
            result={'version':conn.scalar(sa.text('SELECT version()')), 'synthetic':True,'sizes':{'organizations':100,'users':10000,'sessions':2000,'support_tickets':100000,'class_occurrences':100000,'session_enrollments':100000,'orders':50000,'payments':50000,'meeting_email_deliveries':100000},'plans':{}}
            for name, query in queries.items():
                sql = str(query.compile(dialect=conn.dialect,compile_kwargs={'literal_binds':True}))
                result['plans'][name]={'sql':sql,'explain':conn.execute(sa.text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+sql)).scalar()}
            # Measure the actual missing-index case with a rollback-only change.
            # This affects only the synthetic schema and restores the index.
            baseline = conn.begin_nested()
            conn.execute(sa.text('DROP INDEX ix_support_tickets_student_id'))
            sql = str(queries['student_support'].compile(dialect=conn.dialect,compile_kwargs={'literal_binds':True}))
            result['without_student_index'] = conn.execute(sa.text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+sql)).scalar()
            baseline.rollback()
            result['constraints'] = [dict(row) for row in conn.execute(sa.text("""SELECT t.relname AS table_name,c.conname AS name,c.contype AS type,c.convalidated AS validated,pg_get_constraintdef(c.oid) AS definition
                FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
                WHERE n.nspname=current_schema() ORDER BY t.relname,c.conname""")).mappings()]
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,indent=2,default=str)+'\n')
            for name, row in result['plans'].items():
                plan=row['explain'][0]
                print(f"{name}: {plan['Execution Time']} ms; root={plan['Plan']['Node Type']}")
    finally:
        engine.dispose()
        with root.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()


if __name__ == '__main__':
    main()
