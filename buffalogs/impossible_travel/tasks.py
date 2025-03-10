import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from elasticsearch_dsl import Search, connections
from impossible_travel.models import Alert, Login, TaskSettings, User
from impossible_travel.modules import impossible_travel, login_from_new_country, login_from_new_device

logger = logging.getLogger()


def clear_models_periodically():
    """
    Clear DB models
    """
    now = timezone.now()
    delete_user_time = now - timedelta(days=settings.CERTEGO_USER_MAX_DAYS)
    User.objects.filter(updated__lte=delete_user_time).delete()

    delete_login_time = now - timedelta(days=settings.CERTEGO_LOGIN_MAX_DAYS)
    Login.objects.filter(updated__lte=delete_login_time).delete()

    delete_alert_time = now - timedelta(days=settings.CERTEGO_ALERT_MAX_DAYS)
    Alert.objects.filter(updated__lte=delete_alert_time).delete()


@shared_task(name="UpdateRiskLevelTask")
def update_risk_level(new_user):
    with transaction.atomic():
        alerts_num = Alert.objects.filter(user__username=new_user.username).count()
        if alerts_num == 0:
            new_user.risk_score = User.riskScoreEnum.NO_RISK
        elif 1 <= alerts_num <= 2:
            new_user.risk_score = User.riskScoreEnum.LOW
        elif 3 <= alerts_num <= 4:
            new_user.risk_score = User.riskScoreEnum.MEDIUM
        else:
            logger.info(f"{User.riskScoreEnum.HIGH} risk level for User: {new_user.username}")
            new_user.risk_score = User.riskScoreEnum.HIGH
        new_user.save()


def set_alert(db_user, login_alert, alert_info):
    """
    Save the alert on db and logs it
    """
    logger.info(
        f"ALERT {alert_info['alert_name']}\
        for User:{db_user.username}\
        at:{login_alert['timestamp']}\
        from {login_alert['country']}\
        from device:{login_alert['agent']}"
    )
    Alert.objects.create(user_id=db_user.id, login_raw_data=login_alert, name=alert_info["alert_name"], description=alert_info["alert_desc"])


def check_fields(db_user, fields):
    """
    Call the relative function if user_agents and/or geoip exist
    """
    imp_travel = impossible_travel.Impossible_Travel()
    new_dev = login_from_new_device.Login_New_Device()
    new_country = login_from_new_country.Login_New_Country()

    for login in fields:
        if login["lat"] and login["lon"]:
            if Login.objects.filter(user_id=db_user.id).exists():
                agent_alert = False
                country_alert = False
                if login["agent"]:
                    agent_alert = new_dev.check_new_device(db_user, login)
                    if agent_alert:
                        set_alert(db_user, login, agent_alert)

                if login["country"]:
                    country_alert = new_country.check_country(db_user, login)
                    if country_alert:
                        set_alert(db_user, login, country_alert)

                if country_alert or agent_alert:
                    travel_alert = imp_travel.calc_distance(db_user, db_user.login_set.first(), login)
                    if travel_alert:
                        set_alert(db_user, login, travel_alert)
                    imp_travel.add_new_login(db_user, login)
                else:
                    imp_travel.update_model(db_user, login["timestamp"], login["lat"], login["lon"], login["country"], login["agent"])

            else:
                imp_travel.add_new_login(db_user, login)
        else:
            logger.info(f"No lattitude or longitude for User {login}")


def process_user(db_user, start_date, end_date):
    """
    Get info for each user login and normalization
    """
    fields = []
    s = (
        Search(index=settings.CERTEGO_ELASTIC_INDEX)
        .filter("range", **{"@timestamp": {"gte": start_date, "lt": end_date}})
        .query("match", **{"user.name": db_user.username})
        .exclude("match", **{"event.outcome": "failure"})
        .source(includes=["user.name", "@timestamp", "geoip.latitude", "geoip.longitude", "geoip.country_name", "user_agent.original"])
        .sort("@timestamp")
        .extra(size=10000)
    )
    response = s.execute()
    for hit in response:
        tmp = {"timestamp": hit["@timestamp"]}

        if "geoip" in hit and "country_name" in hit["geoip"]:
            tmp["lat"] = hit["geoip"]["latitude"]
            tmp["lon"] = hit["geoip"]["longitude"]
            tmp["country"] = hit["geoip"]["country_name"]
        else:
            tmp["lat"] = None
            tmp["lon"] = None
            tmp["country"] = ""
        if "user_agent" in hit:
            tmp["agent"] = hit["user_agent"]["original"]
        else:
            tmp["agent"] = ""
        fields.append(tmp)
    check_fields(db_user, fields)


@shared_task(name="ProcessLogsTask")
def process_logs():
    """
    Find all user logged in between that time range
    """
    try:
        process_task = TaskSettings.objects.get(task_name=process_logs.__name__)
        start_date = process_task.end_date
        end_date = start_date + timedelta(minutes=30)
        process_task.start_date = start_date
        process_task.end_date = end_date
        process_task.save()
    except ObjectDoesNotExist:
        end_date = timezone.now()
        start_date = end_date + timedelta(minutes=-30)
        TaskSettings.objects.create(task_name=process_logs.__name__, start_date=start_date, end_date=end_date)

    logger = logging.getLogger()
    logger.info(f"Starting at:{start_date} Finishing at:{end_date}")
    connections.create_connection(hosts=settings.CERTEGO_ELASTICSEARCH, timeout=90)
    s = (
        Search(index=settings.CERTEGO_ELASTIC_INDEX)
        .filter("range", **{"@timestamp": {"gte": start_date, "lt": end_date}})
        .exclude("match", **{"event.outcome": "failure"})
    )
    s.aggs.bucket("login_user", "terms", field="user.name", size=10000)
    response = s.execute()

    for user in response.aggregations.login_user.buckets:
        db_user = User.objects.get_or_create(username=user.key)
        process_user(db_user[0], start_date, end_date)
