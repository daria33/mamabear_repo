import time
import logging
import requests
import threading
from dateutil import tz
from dateutil import parser
from datetime import datetime
from mamabear.model import *
from mamabear.docker_wrapper import DockerWrapper
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

logging.basicConfig(level=logging.INFO)

class Worker(object):
    """
    Class that makes use of the docker wrapper to
    update data in the db
    """

    def get_engine(self, config):
        return create_engine('mysql://%s:%s@%s/%s' % (
            config.get('mysql', 'user'),
            config.get('mysql', 'passwd'),
            config.get('mysql', 'host'),
            config.get('mysql', 'database')
        ), echo=False)    

    def get_session(self, engine):    
        sess = scoped_session(sessionmaker(autoflush=True,
                                       autocommit=False))
        sess.configure(bind=engine)
        return sess

    def __init__(self, config, updateOnStart=False):        
        self._config = config
        # Just one registry is allowed for now, and is configured at
        # server launch time
        self._registry_url = config.get('registry', 'host')
        # Registry user is *required*
        self._registry_user = config.get('registry', 'user')
        self._registry_password = config.get('registry', 'password')
        if updateOnStart:            
            self.update_all()
        
    def image_ref(self, app_name, image_tag):
        return "%s/%s:%s" % (self._registry_user, app_name, image_tag)

    def containers_for_app_image(self, db, app_name, image_tag):
        image_ref = self.image_ref(app_name, image_tag)
        return Container.get_by_ref(db, image_ref)
        
    def update_app_images(self, db, app):
        """
        Update images for app from docker registry. If we know
        about existing containers that reference a newly fetched
        image, we attach the containers to the image 
        """
        logging.info("Fetching images for {} from {} ...".format(app.name, self._registry_url))
        images = DockerWrapper.list_images(
            self._registry_url, app.name, self._registry_user, self._registry_password)
        for image_info in images:
            image = Image.get(db, image_info['layer'])
            if image:
                logging.info("Found existing image {}, updating tag to {}".format(image_info['layer'], image_info['name']))
                image.tag = image_info['name']
            else:
                logging.info("Found new image {}, setting tag to {}".format(image_info['layer'], image_info['name']))
                image = Image(id=image_info['layer'], tag=image_info['name'])

            for container in self.containers_for_app_image(db, app.name, image.tag):
                logging.info("Found container {} with state: [{}], associated with image: {}, linking".format(
                    container.id, container.state, image.id
                ))
                image.containers.append(container)
            
            app.images.append(image)
            db.add(image)
            db.add(app)
        db.commit()

    def update_deployment_containers(self, db, deployment, config):
        """
        Update container state for all containers on the deployment's configured hosts.
        """
        for host in deployment.hosts:
            logging.info("Updating containers for host: {}".format(host.hostname))
            self.update_host_containers(db, host, config)

        for container in self.containers_for_app_image(db, deployment.app_name, deployment.image_tag):
            logging.info("Found container {} with state: [{}], associated with deployment: {}, linking".format(
                container.id, container.state, deployment.name()
            ))
            deployment.containers.append(container)
        
        db.add(deployment)
        db.commit()

    def _check_app_status(self, url, timeout=10, retry=3):
        last_exception = None
        for attempt in range(1, retry+1):
            try:
                r = requests.get(url, timeout=timeout)
                if r.ok:
                    return 'up'
                else:
                    return 'down'
            except Exception as e:
                logging.warn("Failed to check status of: {}, reason: [{}]".format(url, e.message))
                time.sleep(5)
                last_exception = e
        else:
            raise last_exception

    def update_deployment(self, db, deployment):
        self.update_deployment_containers(db, deployment, self._config)
        self.update_deployment_status(db, deployment)
                        
    def update_deployment_status(self, db, deployment):
        """
        Update app status for all of the deployment's containers
        """
        for container in deployment.containers:
            if container.state == 'running':
                status_url = "http://%s:%s/%s" % (
                    container.host.hostname,
                    deployment.status_port,
                    deployment.status_endpoint)
                
                logging.info("Checking status of {} for container: {}".format(status_url, container.id))
                try:
                    status = self._check_app_status(status_url)
                    logging.info("Got status of {} for container: {}".format(status, container.id))
                    container.status = status
                except Exception as e:
                    logging.warn("Failed to check status, {}".format(e.message))
                    container.status = 'down'                
            else:
                container.status = 'down'                
            db.add(container)
        db.commit()
        
    def update_host_containers(self, db, host, config):
        """
        For a given host, update the application status and container
        state for all containers.
        """
        wrapper = DockerWrapper(host.hostname, host.port, config)
        host_container_info = []

        # Keep track of containers that go away
        previous_host_containers = dict([(container.id, container) for container in host.containers])
        new_host_containers = set()
        
        try:
            host_container_info = wrapper.state_of_the_universe()
        except Exception as e:
            logging.error(e)
            host.status = 'down'
            db.add(host)
        
        for info in host_container_info:
            container = Container.get(db, info['id'])
            new_host_containers.add(info['id'])
            if container:
                logging.info("Found existing container {}, updating state to: {}".format(info['id'], info['state']))
                container.state = info['state']
                container.finished_at = parser.parse(info['finished_at']).astimezone(tz.tzlocal()).replace(tzinfo=None)
                db.add(container)
            else:
                logging.info("Got new container {}, setting state to: {}".format(info['id'], info['state']))
                image_layer = info['image_id'][0:8]
                image = Image.get(db, image_layer)
                container = Container(
                    id=info['id'],
                    image_ref=info['image_ref'],
                    state = info['state'],
                    started_at = parser.parse(info['started_at']).astimezone(tz.tzlocal()).replace(tzinfo=None),
                    finished_at = parser.parse(info['finished_at']).astimezone(tz.tzlocal()).replace(tzinfo=None)
                )
                if 'command' in info:
                    container.command = info['command']
                if image:
                    container.image = image
                host.containers.append(container)
                db.add(container)

        for c_id in previous_host_containers:
            if c_id not in new_host_containers:
                logging.info("Previous container {} not found on host, removing".format(c_id))
                container = previous_host_containers[c_id]
                db.delete(container)
                
        db.add(host)
        db.commit()
                        
    def update_all_containers(self, db, config):
        """
        Updates every app and container state on all hosts
        """
        for host in db.query(Host).all():
            logging.info("Updating containers for host: {}".format(host.hostname))
            self.update_host_containers(db, host, config)

    def get_container_logs(self, container, config, stderr=True, stdout=False, limit=100):
        wrapper = DockerWrapper(container.host.hostname, container.host.port, config)
        return wrapper.logs(container.id, stdout=stdout, stderr=stderr, tail=limit)
        
    def update_all(self):
        try:
            db = self.get_session(self.get_engine(self._config))
        
            apps = db.query(App).all()
            for app in apps:
                logging.info("Updating image and deployment information for {}".format(app.name))
                try:
                    self.update_app_images(db, app)
                except Exception as e:
                    logging.error(e)
                    db.rollback()
                
                for deployment in app.deployments:
                    try:
                        self.update_deployment(db, deployment)
                    except Exception as e:
                        logging.error(e)
                        db.rollback()
                    
            logging.info("Updating container information")
            try:
                self.update_all_containers(db, self._config)
            except Exception as e:
                logging.error(e)
                db.rollback()
        except Exception as e:
            logging.error(e)
            pass

    def run_deployment(self, deployment_id):
        thread = threading.Thread(target=self.launch_deployment, args=([deployment_id, self._config]))
        thread.daemon = True
        thread.start()
        
    def launch_deployment(self, deployment_id, config):
        db = self.get_session(self.get_engine(config))
        
        deployment = db.query(Deployment).get(deployment_id)
        encoded = deployment.encode_with_deps(db)
        logging.info("Launching deployment {}:{}/{}".format(deployment.app_name, deployment.image_tag, deployment.environment))
        for host in deployment.hosts:
            logging.info("Launching deployment {}:{}/{} on {}".format(deployment.app_name, deployment.image_tag, deployment.environment, host.alias))
            wrapper = DockerWrapper(host.hostname, host.port, config)
            wrapper.deploy_with_deps(encoded)

        try:
            self.update_deployment_containers(db, deployment, config)
            self.update_deployment_status(db, deployment)            
        except Exception as e:
            logging.error(e)
            db.rollback()
        logging.info("Finished deployment {}:{}/{}".format(deployment.app_name, deployment.image_tag, deployment.environment))
