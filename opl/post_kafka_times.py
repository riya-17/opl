import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import threading
import time

from kafka import KafkaProducer

import opl.args
import opl.data
import opl.db
import opl.skelet

import psycopg2
import psycopg2.extras

import yaml


"""
You want to use this helper if you want to achieve this:

    * you have a messages generator like the in OPL `generators/`
    * you want to produce them to Kafka topic
    * you want to record when each message was sent

To create a script using this helper, you can create this:

    import json
    import socket

    import opl.generators.inventory_egress
    import opl.post_kafka_times


    def func_add_more_args(parser):
        # This function allows to add more parameters to the tool besides
        # what opl.post_kafka_times.post_kafka_times defines - maybe because
        # you will need it later in func_return_generator.
        parser.add_argument(
            "--count",
            default=100,
            type=int,
            help="How many messages to prepare",
        )
        parser.add_argument(
            "--n-packages",
            default=500,
            type=int,
            help="How many packages addresses should each host have",
        )
        parser.add_argument(
            "--msg-type",
            default="created",
            choices=["created"],
            help="Type of the message",
        )


    def func_return_generator(args):
        # This function is supposed to return the generator and is given
        # arguments passed on command line.
        # Generator is supposed to return touple with message ID and message
        # when iterating over it.
        return opl.generators.inventory_egress.EgressHostsGenerator(
            count=args.count,
            n_packages=args.n_packages,
            msg_type=args.msg_type,
        )


    def func_return_message_payload(args, message_id, message):
        # This function is supposed to return payload usable by Kafka
        # producer when sending - i.e. simple `string`. Helper will just
        # encode it into `bytes`. If your producer returns strings, you
        # might go with just `return message`.
        # This function have access to arguments from argparse
        # and message_id and message as provided by generator.
        return json.dumps(message)


    def func_return_message_key(args, message_id, message):
        # This function is supposed to return message key. Just
        # `return None` if your topic/app does not require it.
        return message_id


    def func_return_message_headers(args, message_id, message):
        # This function is supposed to return message headers if your
        # app/topic needs it. If you do not need it, just use `return []`.
        _event_type = args.msg_type
        _request_id = message["platform_metadata"]["request_id"]
        _producer = socket.gethostname()
        _insights_id = message["host"]["insights_id"]
        return [
            ("event_type", _event_type),
            ("request_id", _request_id),
            ("producer", _producer),
            ("insights_id", _insights_id),
        ]


    if __name__ == "__main__":
        # Here we just create config for the helper...
        config = {
            "func_add_more_args": func_add_more_args,
            "query_store_info_produced": "query_store_info_produced",
            "func_return_generator": func_return_generator,
            "func_return_message_payload": func_return_message_payload,
            "func_return_message_headers": func_return_message_headers,
            "func_return_message_key": func_return_message_key,
        }
        # ...and run the helper
        opl.post_kafka_times.post_kafka_times(config)
"""


class PostKafkaTimes:
    """
    This class is meant to encapsulate producing generated messages to Kafka
    and record ID of the message and timestamp of when each message was sent
    to sorage DB.
    """

    def __init__(self, args, config, produce_here, save_here):
        """
        Connect to the storage DB, load SQL query templates, initiate
        a BatchProcessor, instantate messages generator and start
        a Kafka producer.
        """
        self.args = args
        self.config = config
        self.produce_here = produce_here
        self.save_here = save_here
        self.kafka_topic = args.kafka_topic
        self.show_processed_messages = args.show_processed_messages
        self.rate = args.rate

        logging.info("Creating generator")
        self.generator = self.config["func_return_generator"](args)

    def dt_now(self):
        """
        Return current time in UTC timezone with UTC timezone.
        """
        return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

    def work(self):
        """
        Produce messages generated by generator to Kafka using the producer.
        """

        def handle_send_success(*args, **kwargs):
            self.save_here.add((kwargs["message_id"], self.dt_now()))

        def handle_send_error(e, message_id):
            logging.error(f"Failed to produce message {message_id}", exc_info=e)

        def wait_for_next_second(second=int(time.perf_counter())):
            while second == int(time.perf_counter()):
                time.sleep(0.01)
            return int(time.perf_counter())

        logging.info("Started message generation")

        in_second = 0  # how many messages we have sent in this second
        this_second = wait_for_next_second()  # second relevant for in_second

        for message_id, message in self.generator:
            # If we have this function defined, allow custom message_id
            if "func_return_message_id" in self.config:
                message_id = self.config["func_return_message_id"](
                    self.args, message_id, message
                )

            # Message payload
            value = self.config["func_return_message_payload"](
                self.args, message_id, message
            )
            send_params = {"value": value.encode("UTF-8")}

            # Do we need message key?
            key = self.config["func_return_message_key"](self.args, message_id, message)
            if key is not None:
                send_params["key"] = key.encode("UTF-8")

            # Do we need message headers?
            headers = self.config["func_return_message_headers"](
                self.args, message_id, message
            )
            send_params["headers"] = [(h, k.encode("UTF-8")) for h, k in headers]

            # Show message if we wanted it
            if self.show_processed_messages:
                print(f"Producing {json.dumps(send_params, sort_keys=True)}")

            future = self.produce_here.send(self.kafka_topic, **send_params)
            future.add_callback(handle_send_success, message_id=message_id)
            future.add_errback(handle_send_error, message_id=message_id)

            if int(time.perf_counter()) == this_second:
                in_second += 1
                if in_second == self.rate:
                    logging.debug(f"In second {this_second} sent {in_second} messages")
                    this_second = wait_for_next_second(this_second)
                    in_second = 0
            else:
                if self.rate != 0 and self.rate != in_second:
                    logging.warning(
                        f"In second {this_second} sent {in_second} messages (but wanted to send {self.rate})"
                    )
                    this_second = int(time.perf_counter())
                    in_second = 0

        logging.info("Finished message generation, producing and storing")


def post_kafka_times(config):
    """
    This is the main helper function you should use in your code.
    It handles arguments, opening status data file and running
    the actual workload.
    """
    parser = argparse.ArgumentParser(
        description="Given a Kafka messages generator produce messages and put timestamps into DB",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--kafka-topic",
        default=os.getenv("KAFKA_TOPIC", "platform.upload.qpc"),
        help="Produce to this topic (also use env variable KAFKA_TOPIC)",
    )
    parser.add_argument(
        "--kafka-producer-threads",
        type=int,
        default=os.getenv("KAFKA_PRODUCER_THREADS", 1),
        help="Produce in this many threads (also use env variable KAFKA_PRODUCER_THREADS)",
    )
    parser.add_argument(
        "--show-processed-messages",
        action="store_true",
        help="Show messages we are producing",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=0,
        help="How many messages per second should we produce (0 for no limit)",
    )

    opl.args.add_storage_db_opts(parser)
    opl.args.add_kafka_opts(parser)
    opl.args.add_tables_def_opts(parser)

    # PostKafkaTimes needs this config keys in config dict
    assert "query_store_info_produced" in config
    assert "func_return_generator" in config
    assert "func_add_more_args" in config
    assert "func_return_message_payload" in config
    assert "func_return_message_key" in config
    assert "func_return_message_headers" in config

    # Add more args
    config["func_add_more_args"](parser)

    def produce_thread(args, config, produce_here, save_here):
        produce_object = PostKafkaTimes(args, config, produce_here, save_here)
        return produce_object.work()

    with opl.skelet.test_setup(parser) as (args, status_data):
        # Sanitize and include args into status data file
        args_copy = vars(args).copy()
        args_copy["tables_definition"] = args_copy["tables_definition"].name
        status_data.set("parameters.produce_messages", args_copy)

        # Sanitize acks setting
        if args.kafka_acks != "all":
            args.kafka_acks = int(args.kafka_acks)

        # Common parameters for both cases
        common_params = {
            "bootstrap_servers": [f"{args.kafka_host}:{args.kafka_port}"],
            "acks": args.kafka_acks,
            "retries": args.kafka_retries,
            "batch_size": args.kafka_batch_size,
            "buffer_memory": args.kafka_buffer_memory,
            "linger_ms": args.kafka_linger_ms,
            "max_block_ms": args.kafka_max_block_ms,
            "request_timeout_ms": args.kafka_request_timeout_ms,
            "compression_type": args.kafka_compression_type,
        }

        if args.kafka_username != "" and args.kafka_password != "":
            logging.info(
                f"Creating SASL password-protected producer to {args.kafka_host}"
            )
            sasl_params = {
                "security_protocol": "SASL_SSL",
                "sasl_mechanism": "SCRAM-SHA-512",
                "sasl_plain_username": args.kafka_username,
                "sasl_plain_password": args.kafka_password,
            }
            produce_here = KafkaProducer(**common_params, **sasl_params)
        else:
            logging.info(
                f"Creating passwordless producer to {args.kafka_host}:{args.kafka_port}"
            )
            produce_here = KafkaProducer(**common_params)

        logging.info(f"Loading queries definition from {args.tables_definition}")
        queries_definition = yaml.load(args.tables_definition, Loader=yaml.SafeLoader)[
            "queries"
        ]

        storage_db_conf = {
            "host": args.storage_db_host,
            "port": args.storage_db_port,
            "database": args.storage_db_name,
            "user": args.storage_db_user,
            "password": args.storage_db_pass,
        }
        storage_db_connection = psycopg2.connect(**storage_db_conf)
        sql = queries_definition[config["query_store_info_produced"]]
        data_lock = threading.Lock()
        logging.info(f"Creating storage DB batch inserter with {sql}")
        save_here = opl.db.BatchProcessor(
            storage_db_connection, sql, batch=100, lock=data_lock
        )

        status_data.set_now("parameters.produce.started_at")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.kafka_producer_threads
        ) as executor:
            my_threads = [
                executor.submit(produce_thread, args, config, produce_here, save_here)
                for i in range(args.kafka_producer_threads)
            ]
            for future in concurrent.futures.as_completed(my_threads):
                try:
                    future.result()
                except Exception as exc:
                    logging.info(f"Thread {future} caused exception: {exc}")
                    logging.exception(exc)
                else:
                    logging.info(f"Thread {future} worked")

        produce_here.flush()

        status_data.set_now("parameters.produce.ended_at")

        save_here.commit()
